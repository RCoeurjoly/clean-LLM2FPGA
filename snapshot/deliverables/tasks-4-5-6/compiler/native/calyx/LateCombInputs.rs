//! Hoist private comb-group inputs AFTER CellShare. Opt-in: binary control only.
//!
//! Cells, control, groups and register allocation are unchanged. An inactive
//! private combinational result becomes unobservable rather than zero. This
//! preserves legal 0/1 control executions, not arbitrary X activation traces.
use crate::traversal::{
    Action, ConstructVisitor, Named, ParseVal, PassOpt, VisResult, Visitor,
};
use calyx_frontend::{NamespaceDef, Primitive, PrimitiveInfo};
use calyx_ir::{self as ir, BoolAttr};
use calyx_utils::CalyxResult;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;

pub struct LateCombInputs {
    enabled: bool,
    trusted: HashSet<ir::Id>,
}

impl Named for LateCombInputs {
    fn name() -> &'static str { "late-comb-inputs" }
    fn description() -> &'static str {
        "hoist private trusted condition inputs after register sharing (binary control only)"
    }
    fn opts() -> Vec<PassOpt> {
        vec![PassOpt::new(
            "assume-binary-control",
            "explicitly restrict equivalence to known 0/1 control execution",
            ParseVal::Bool(false), PassOpt::parse_bool,
        )]
    }
}

fn same_primitive(a: &Primitive, b: &Primitive) -> bool {
    a.name == b.name && a.params == b.params && a.attributes == b.attributes
        && a.is_comb == b.is_comb && a.latency == b.latency && a.body == b.body
        && a.signature.len() == b.signature.len()
        && a.signature.iter().zip(&b.signature).all(|(x, y)| {
            x.name() == y.name() && x.width == y.width
                && x.direction == y.direction && x.attributes == y.attributes
        })
}

// Authenticate declarations AND implementations, not names or `is_comb` alone.
// Native parsing compares pinned source declarations; exact bytes authenticate
// extern RTL even when its installation prefix differs from this executable.
fn trusted_primitives(ctx: &ir::Context) -> CalyxResult<HashSet<ir::Id>> {
    let sources: [(&str, Option<&[u8]>); 3] = [
        (include_str!("../../../../primitives/compile.futil"), None),
        (include_str!("../../../../primitives/core.futil"),
         Some(include_bytes!("../../../../primitives/core.sv"))),
        (include_str!("../../../../primitives/binary_operators.futil"),
         Some(include_bytes!("../../../../primitives/binary_operators.sv"))),
    ];
    let mut expected = HashMap::new();
    for (source, rtl) in sources {
        for (path, primitives) in NamespaceDef::construct_from_str(source)?.externs {
            for primitive in primitives {
                if primitive.is_comb {
                    expected.insert(primitive.name, (primitive, if path.is_some() { rtl } else { None }));
                }
            }
        }
    }
    let mut trusted = HashSet::new();
    for info in ctx.lib.prim_infos() {
        match info {
            PrimitiveInfo::Inline { primitive, .. } => {
                if let Some((original, None)) = expected.get(&primitive.name) {
                    if same_primitive(primitive, original) { trusted.insert(primitive.name); }
                }
            }
            PrimitiveInfo::Extern { path, primitives, .. } => {
                // Bound file reads before loading a possibly user-defined extern.
                let length = std::fs::metadata(path).ok().map(|m| m.len());
                let mut bytes = None;
                for primitive in primitives.values() {
                    if let Some((original, Some(rtl))) = expected.get(&primitive.name) {
                        if same_primitive(primitive, original) && length == Some(rtl.len() as u64) {
                            let actual = bytes.get_or_insert_with(|| std::fs::read(path).ok());
                            if actual.as_deref() == Some(*rtl) { trusted.insert(primitive.name); }
                        }
                    }
                }
            }
        }
    }
    Ok(trusted)
}

impl ConstructVisitor for LateCombInputs {
    fn from(ctx: &ir::Context) -> CalyxResult<Self> {
        let enabled = Self::get_opts(ctx)["assume-binary-control"].bool()
            && ctx.metadata.is_none();
        Ok(Self { enabled, trusted: if enabled { trusted_primitives(ctx)? } else { HashSet::new() } })
    }
    fn clear_data(&mut self) {}
}

fn port_key(port: &ir::RRC<ir::Port>) -> usize { Rc::as_ptr(port) as usize }
fn group_key(group: &ir::RRC<ir::CombGroup>) -> usize { Rc::as_ptr(group) as usize }

#[derive(Clone, Copy)]
enum Write {
    Comb { group: usize, index: usize, plain: bool },
    Continuous { plain: bool },
    Other,
}

#[derive(Default)]
struct Uses {
    writes: HashMap<usize, Vec<Write>>,
    reads: HashMap<usize, Vec<Option<usize>>>,
    invoked_groups: HashSet<usize>,
}

impl Uses {
    fn read(&mut self, port: &ir::RRC<ir::Port>, scope: Option<usize>) {
        self.reads.entry(port_key(port)).or_default().push(scope);
    }
    fn assignment(&mut self, a: &ir::Assignment<ir::Nothing>, write: Write, scope: Option<usize>) {
        self.writes.entry(port_key(&a.dst)).or_default().push(write);
        self.read(&a.src, scope);
        for port in a.guard.all_ports() { self.read(&port, scope); }
    }
    // Hidden ref bindings, static control and precompiled FSMs are deliberately
    // unsupported. Ordinary invokes expose every bound port conservatively.
    // No move is made before this traversal has fully succeeded.
    fn control(&mut self, control: &ir::Control) -> bool {
        match control {
            ir::Control::Seq(c) => c.stmts.iter().all(|c| self.control(c)),
            ir::Control::Par(c) => c.stmts.iter().all(|c| self.control(c)),
            ir::Control::If(c) => {
                self.read(&c.port, c.cond.as_ref().map(group_key));
                self.control(&c.tbranch) && self.control(&c.fbranch)
            }
            ir::Control::While(c) => {
                self.read(&c.port, c.cond.as_ref().map(group_key));
                self.control(&c.body)
            }
            ir::Control::Invoke(c) => {
                if !c.ref_cells.is_empty() { return false; }
                if let Some(group) = &c.comb_group {
                    self.invoked_groups.insert(group_key(group));
                }
                for (_, port) in &c.inputs { self.read(port, None); }
                for (_, port) in &c.outputs {
                    self.writes.entry(port_key(port)).or_default().push(Write::Other);
                }
                // Invocation also drives/observes implicit handshake ports.
                for port in &c.comp.borrow().ports {
                    match port.borrow().direction {
                        ir::Direction::Input => {
                            self.writes.entry(port_key(port)).or_default().push(Write::Other);
                        }
                        _ => self.read(port, None),
                    }
                }
                true
            }
            ir::Control::Enable(_) | ir::Control::Empty(_) => true,
            _ => false,
        }
    }
}

impl Visitor for LateCombInputs {
    fn start(&mut self, comp: &mut ir::Component, _sigs: &ir::LibrarySignatures,
             _comps: &[ir::Component]) -> VisResult {
        if !self.enabled || !comp.static_groups.is_empty() || !comp.fsms.is_empty() {
            return Ok(Action::Stop);
        }
        let mut uses = Uses::default();
        if !uses.control(&comp.control.borrow()) { return Ok(Action::Stop); }
        for a in &comp.continuous_assignments {
            uses.assignment(a, Write::Continuous { plain: a.guard.is_true() && a.attributes.is_empty() }, None);
        }
        for g in comp.groups.iter() {
            for a in &g.borrow().assignments { uses.assignment(a, Write::Other, None); }
        }
        let groups: Vec<_> = comp.comb_groups.iter().cloned().collect();
        let mut group_attributes = HashMap::new();
        for group in &groups {
            let key = group_key(group);
            let g = group.borrow();
            group_attributes.insert(key, g.attributes.is_empty());
            for (index, a) in g.assignments.iter().enumerate() {
                uses.assignment(a, Write::Comb { group: key, index,
                    plain: a.guard.is_true() && a.attributes.is_empty() }, Some(key));
            }
        }
        let mut plan: HashMap<usize, HashSet<usize>> = HashMap::new();
        let mut selected_cells = 0usize;
        for cell in comp.cells.iter() {
            let cell = cell.borrow();
            if cell.is_reference() { continue; }
            let ir::CellType::Primitive { name, is_comb: true, .. } = &cell.prototype else { continue; };
            if !self.trusted.contains(name) { continue; }
            let mut attributes = cell.attributes.clone();
            attributes.remove(BoolAttr::Data); attributes.remove(BoolAttr::Control);
            if !attributes.is_empty() { continue; }
            let mut scope = None;
            let mut moves = Vec::new();
            let mut valid = true;
            let mut outputs: Vec<Option<usize>> = Vec::new();
            for port in &cell.ports {
                let key = port_key(port);
                match port.borrow().direction {
                    ir::Direction::Input => {
                        if uses.reads.contains_key(&key) { valid = false; break; }
                        match uses.writes.get(&key).map(Vec::as_slice) {
                            Some([Write::Comb { group, index, plain: true }]) => {
                                if scope.is_some_and(|s| s != *group) { valid = false; break; }
                                scope = Some(*group); moves.push((*group, *index));
                            }
                            Some([Write::Continuous { plain: true }]) => {},
                            _ => { valid = false; break; }
                        }
                    }
                    ir::Direction::Output => {
                        if uses.writes.contains_key(&key) { valid = false; break; }
                        if let Some(reads) = uses.reads.get(&key) { outputs.extend(reads.iter().copied()); }
                    }
                    _ => { valid = false; break; }
                }
            }
            if !valid || moves.is_empty() || outputs.is_empty() { continue; }
            let Some(scope) = scope else { continue; };
            if uses.invoked_groups.contains(&scope) || group_attributes.get(&scope) != Some(&true)
                || outputs.iter().any(|&read| read != Some(scope)) { continue; }
            selected_cells += 1;
            for (group, index) in moves { plan.entry(group).or_default().insert(index); }
        }
        let moved_assignments: usize = plan.values().map(HashSet::len).sum();
        for group in groups {
            if let Some(indices) = plan.get(&group_key(&group)) {
                let mut group = group.borrow_mut();
                for (index, a) in std::mem::take(&mut group.assignments).into_iter().enumerate() {
                    if indices.contains(&index) { comp.continuous_assignments.push(a); }
                    else { group.assignments.push(a); }
                }
            }
        }
        log::info!("late-comb-inputs component={} cells={} assignments={}",
                   comp.name, selected_cells, moved_assignments);
        Ok(Action::Stop)
    }
}
