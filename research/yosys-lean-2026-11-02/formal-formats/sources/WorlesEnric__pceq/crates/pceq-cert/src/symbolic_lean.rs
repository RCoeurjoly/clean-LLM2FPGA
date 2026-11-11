use std::{collections::BTreeMap, fmt::Write as _};

use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::{LeanCheckerFingerprint, VcArtifact, VcKind};

pub const SYMBOLIC_LEAN_SCHEMA_VERSION: u32 = 1;
pub const SYMBOLIC_LEAN_CHECKER: &str = "lean-bv_decide-lrat-v1";

/// One exact symbolic VC reconstructed by a generated Lean theorem.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SymbolicLeanManifestEntry {
    pub vc_digest: String,
    pub problem_digest: String,
    pub product_hash: String,
    pub certificate_hash: String,
    pub theorem: String,
    pub kind: VcKind,
    pub checker: String,
}

/// Deterministic Lean source and its digest-sorted content manifest.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SymbolicLeanModule {
    pub schema_version: u32,
    pub namespace: String,
    pub source: String,
    pub manifest: Vec<SymbolicLeanManifestEntry>,
}

impl SymbolicLeanModule {
    #[must_use]
    pub fn source_digest(&self) -> String {
        blake3::hash(self.source.as_bytes()).to_hex().to_string()
    }

    pub fn manifest_digest(&self) -> String {
        pceq_core::canonical_hash("pceq/symbolic-lean-manifest/v1", &self.manifest)
            .expect("symbolic Lean manifest canonical serialization")
            .to_hex()
    }

    /// Reparse and rerender the exact VCs before crediting this module.
    pub fn validate_exact(&self, vcs: &[VcArtifact]) -> Result<(), SymbolicLeanError> {
        if self.schema_version != SYMBOLIC_LEAN_SCHEMA_VERSION {
            return Err(SymbolicLeanError::Schema(self.schema_version));
        }
        let expected = export_symbolic_vc_module(&self.namespace, vcs)?;
        if *self != expected {
            return Err(SymbolicLeanError::ExactRerender);
        }
        Ok(())
    }
}

/// Content-addressed evidence for one exact module and one exact Lean toolchain.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct SymbolicLeanAuditArtifact {
    pub schema_version: u32,
    pub checker: LeanCheckerFingerprint,
    pub module: SymbolicLeanModule,
}

impl SymbolicLeanAuditArtifact {
    pub fn new(
        checker: LeanCheckerFingerprint,
        module: SymbolicLeanModule,
        vcs: &[VcArtifact],
    ) -> Result<Self, SymbolicLeanError> {
        let artifact = Self {
            schema_version: SYMBOLIC_LEAN_SCHEMA_VERSION,
            checker,
            module,
        };
        artifact.validate_exact(vcs)?;
        Ok(artifact)
    }

    #[must_use]
    pub fn checker_digest(&self) -> String {
        self.checker.digest()
    }

    pub fn digest(&self) -> String {
        pceq_core::canonical_hash("pceq/symbolic-lean-audit/v1", self)
            .expect("symbolic Lean audit canonical serialization")
            .to_hex()
    }

    pub fn validate_exact(&self, vcs: &[VcArtifact]) -> Result<(), SymbolicLeanError> {
        if self.schema_version != SYMBOLIC_LEAN_SCHEMA_VERSION {
            return Err(SymbolicLeanError::Schema(self.schema_version));
        }
        self.checker
            .validate()
            .map_err(|error| SymbolicLeanError::Checker(error.to_string()))?;
        self.module.validate_exact(vcs)
    }
}

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum SymbolicLeanError {
    #[error("unsupported symbolic Lean schema version {0}")]
    Schema(u32),
    #[error("invalid Lean namespace {0:?}")]
    InvalidNamespace(String),
    #[error("cannot export an empty symbolic VC list")]
    Empty,
    #[error("duplicate VC digest {0}")]
    DuplicateDigest(String),
    #[error("VC {digest} is structurally invalid: {reason}")]
    InvalidArtifact { digest: String, reason: String },
    #[error("VC {digest} has non-content-addressed {field}")]
    InvalidLinkageDigest { digest: String, field: &'static str },
    #[error("VC {digest} kind {kind:?} is outside the Phase-1 initial/consecution fragment")]
    UnsupportedKind { digest: String, kind: VcKind },
    #[error("VC {digest} uses unsupported symbolic SMT: {reason}")]
    UnsupportedSmt { digest: String, reason: String },
    #[error("symbolic Lean source/manifest is not the exact deterministic rerender")]
    ExactRerender,
    #[error("invalid Lean checker fingerprint: {0}")]
    Checker(String),
}

/// Translate the Phase-1 `QF_BV` initial-safety/consecution fragment to theorem
/// statements over free `BitVec`/`Bool` variables. `bv_decide` then bit-blasts
/// those symbolic statements and checks the generated LRAT proof in Lean.
pub fn export_symbolic_vc_module(
    namespace: &str,
    vcs: &[VcArtifact],
) -> Result<SymbolicLeanModule, SymbolicLeanError> {
    validate_namespace(namespace)?;
    if vcs.is_empty() {
        return Err(SymbolicLeanError::Empty);
    }

    let mut parsed = BTreeMap::new();
    for vc in vcs {
        let digest = vc.digest();
        vc.validate()
            .map_err(|error| SymbolicLeanError::InvalidArtifact {
                digest: digest.clone(),
                reason: error.to_string(),
            })?;
        for (field, value) in [
            ("product_hash", vc.product_hash.as_str()),
            ("certificate_hash", vc.certificate_hash.as_str()),
        ] {
            if !is_digest(value) {
                return Err(SymbolicLeanError::InvalidLinkageDigest {
                    digest: digest.clone(),
                    field,
                });
            }
        }
        if !matches!(
            vc.kind,
            VcKind::InitialSafety | VcKind::EdgeConsecution | VcKind::InvariantConsecution
        ) {
            return Err(SymbolicLeanError::UnsupportedKind {
                digest,
                kind: vc.kind,
            });
        }
        let reconstruction =
            Reconstruction::parse(&vc.declarations, &vc.formula).map_err(|reason| {
                SymbolicLeanError::UnsupportedSmt {
                    digest: digest.clone(),
                    reason,
                }
            })?;
        if parsed
            .insert(digest.clone(), (vc, reconstruction))
            .is_some()
        {
            return Err(SymbolicLeanError::DuplicateDigest(digest));
        }
    }

    let mut source = format!(
        "import Pceq.Vc.Symbolic\n\nnamespace {namespace}\n\n-- Generated from exact canonical PCEQ symbolic VC artifacts.\n"
    );
    let mut manifest = Vec::with_capacity(parsed.len());
    for (digest, (vc, reconstruction)) in parsed {
        manifest.push(render_vc(&mut source, &digest, vc, &reconstruction));
    }
    writeln!(source, "\nend {namespace}").expect("writing to String cannot fail");
    Ok(SymbolicLeanModule {
        schema_version: SYMBOLIC_LEAN_SCHEMA_VERSION,
        namespace: namespace.into(),
        source,
        manifest,
    })
}

fn render_vc(
    source: &mut String,
    digest: &str,
    vc: &VcArtifact,
    reconstruction: &Reconstruction,
) -> SymbolicLeanManifestEntry {
    let problem_digest = blake3::hash(vc.smt2_validity_query().as_bytes())
        .to_hex()
        .to_string();
    let theorem = format!("vc_{digest}_accepted");
    write!(
        source,
        "\n-- SMT validity-query digest: {problem_digest}\ntheorem {theorem}"
    )
    .expect("writing to String cannot fail");
    for parameter in &reconstruction.parameters {
        write!(
            source,
            "\n    ({} : {})",
            parameter.name,
            parameter.sort.to_lean()
        )
        .expect("writing to String cannot fail");
    }
    write!(
        source,
        " :\n    Pceq.Vc.Symbolic.BoundVc\n      \"{digest}\"\n      \"{problem_digest}\"\n      \"{}\"\n      \"{}\"\n      (",
        vc.product_hash, vc.certificate_hash
    )
    .expect("writing to String cannot fail");
    for definition in &reconstruction.definitions {
        write!(
            source,
            "let {} : {} := {}\n       ",
            definition.name,
            definition.sort.to_lean(),
            definition.value
        )
        .expect("writing to String cannot fail");
    }
    write!(
        source,
        "{} = true) := by\n  unfold Pceq.Vc.Symbolic.BoundVc\n  bv_decide\n",
        reconstruction.formula
    )
    .expect("writing to String cannot fail");
    SymbolicLeanManifestEntry {
        vc_digest: digest.into(),
        problem_digest,
        product_hash: vc.product_hash.clone(),
        certificate_hash: vc.certificate_hash.clone(),
        theorem,
        kind: vc.kind,
        checker: SYMBOLIC_LEAN_CHECKER.into(),
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Sort {
    Bool,
    BitVec(usize),
}

impl Sort {
    fn to_lean(self) -> String {
        match self {
            Self::Bool => "Bool".into(),
            Self::BitVec(width) => format!("BitVec {width}"),
        }
    }
}

#[derive(Debug, Clone)]
struct Binding {
    sort: Sort,
    name: String,
}

#[derive(Debug, Clone)]
struct Definition {
    name: String,
    sort: Sort,
    value: String,
}

#[derive(Debug, Clone)]
struct Reconstruction {
    parameters: Vec<Binding>,
    definitions: Vec<Definition>,
    formula: String,
}

impl Reconstruction {
    fn parse(declarations: &str, formula: &str) -> Result<Self, String> {
        let commands = parse_many(declarations)?;
        let formula = parse_one(formula)?;
        let mut environment = BTreeMap::new();
        let mut parameters = Vec::new();
        let mut definitions = Vec::new();
        for command in commands {
            let items = command.list("declaration command")?;
            let head = items
                .first()
                .ok_or_else(|| "empty declaration command".to_string())?
                .atom("declaration head")?;
            match head {
                "declare-fun" => {
                    if items.len() != 4 || !items[2].list("function arguments")?.is_empty() {
                        return Err("only zero-arity declare-fun is supported".into());
                    }
                    let symbol = items[1].atom("declared symbol")?;
                    let sort = parse_sort(&items[3])?;
                    let name = format!("v{}", parameters.len());
                    insert_binding(&mut environment, symbol, sort, name.clone())?;
                    parameters.push(Binding { sort, name });
                }
                "declare-const" => {
                    if items.len() != 3 {
                        return Err("malformed declare-const".into());
                    }
                    let symbol = items[1].atom("declared symbol")?;
                    let sort = parse_sort(&items[2])?;
                    let name = format!("v{}", parameters.len());
                    insert_binding(&mut environment, symbol, sort, name.clone())?;
                    parameters.push(Binding { sort, name });
                }
                "define-fun" => {
                    if items.len() != 5 || !items[2].list("function arguments")?.is_empty() {
                        return Err("only zero-arity define-fun is supported".into());
                    }
                    let symbol = items[1].atom("defined symbol")?;
                    let expected = parse_sort(&items[3])?;
                    let rendered = render(&items[4], &environment)?;
                    if rendered.sort != expected {
                        return Err(format!("definition {symbol:?} has the wrong sort"));
                    }
                    let name = format!("d{}", definitions.len());
                    insert_binding(&mut environment, symbol, expected, name.clone())?;
                    definitions.push(Definition {
                        name,
                        sort: expected,
                        value: rendered.text,
                    });
                }
                other => return Err(format!("unsupported declaration command {other:?}")),
            }
        }
        let formula = render(&formula, &environment)?;
        if formula.sort != Sort::Bool {
            return Err("VC formula is not Boolean".into());
        }
        Ok(Self {
            parameters,
            definitions,
            formula: formula.text,
        })
    }
}

fn insert_binding(
    environment: &mut BTreeMap<String, Binding>,
    symbol: &str,
    sort: Sort,
    name: String,
) -> Result<(), String> {
    if environment
        .insert(symbol.to_string(), Binding { sort, name })
        .is_some()
    {
        return Err(format!("duplicate declaration for {symbol:?}"));
    }
    Ok(())
}

#[derive(Debug, Clone)]
struct Rendered {
    sort: Sort,
    text: String,
}

fn render(expression: &SExpr, environment: &BTreeMap<String, Binding>) -> Result<Rendered, String> {
    match expression {
        SExpr::Atom(atom) => render_atom(atom, environment),
        SExpr::List(items) => render_list(items, environment),
    }
}

fn render_atom(atom: &str, environment: &BTreeMap<String, Binding>) -> Result<Rendered, String> {
    match atom {
        "true" | "false" => Ok(Rendered {
            sort: Sort::Bool,
            text: atom.into(),
        }),
        _ if atom.starts_with("#b") => render_based_literal(&atom[2..], 1),
        _ if atom.starts_with("#x") => render_based_literal(&atom[2..], 4),
        _ => environment
            .get(atom)
            .map(|binding| Rendered {
                sort: binding.sort,
                text: binding.name.clone(),
            })
            .ok_or_else(|| format!("undeclared or unsupported atom {atom:?}")),
    }
}

fn render_based_literal(digits: &str, bits_per_digit: usize) -> Result<Rendered, String> {
    if digits.is_empty() {
        return Err("empty bit-vector literal".into());
    }
    let radix = if bits_per_digit == 1 { 2 } else { 16 };
    let value = u128::from_str_radix(digits, radix)
        .map_err(|_| format!("invalid base-{radix} bit-vector literal"))?;
    let width = digits
        .len()
        .checked_mul(bits_per_digit)
        .ok_or_else(|| "bit-vector width overflow".to_string())?;
    Ok(bitvec_literal(value, width))
}

fn render_list(
    items: &[SExpr],
    environment: &BTreeMap<String, Binding>,
) -> Result<Rendered, String> {
    let Some(head) = items.first() else {
        return Err("empty SMT expression".into());
    };
    if let SExpr::List(indexed) = head {
        return render_indexed(indexed, &items[1..], environment);
    }
    let operator = head.atom("operator")?;
    let arguments = &items[1..];
    match operator {
        "_" => render_indexed_literal(arguments),
        "not" => unary_bool("!", arguments, environment),
        "and" => nary_bool("&&", "true", arguments, environment),
        "or" => nary_bool("||", "false", arguments, environment),
        "xor" => nary_xor(arguments, environment),
        "=>" => implication(arguments, environment),
        "=" => equality(arguments, environment, false),
        "distinct" => equality(arguments, environment, true),
        "ite" => ite(arguments, environment),
        "bvnot" => unary_bv("~~~", arguments, environment),
        "bvneg" => unary_bv("-", arguments, environment),
        "bvadd" => binary_bv("+", arguments, environment),
        "bvsub" => binary_bv("-", arguments, environment),
        "bvmul" => binary_bv("*", arguments, environment),
        "bvand" => binary_bv("&&&", arguments, environment),
        "bvor" => binary_bv("|||", arguments, environment),
        "bvxor" => binary_bv("^^^", arguments, environment),
        "bvudiv" => binary_bv_call("BitVec.udiv", arguments, environment),
        "bvsdiv" => binary_bv_call("BitVec.sdiv", arguments, environment),
        "bvurem" => binary_bv_call("BitVec.umod", arguments, environment),
        "bvsrem" => binary_bv_call("BitVec.srem", arguments, environment),
        "bvsmod" => binary_bv_call("BitVec.smod", arguments, environment),
        "bvshl" => shift_bv("<<<", arguments, environment),
        "bvlshr" => shift_bv(">>>", arguments, environment),
        "concat" => concat(arguments, environment),
        "bvult" => comparison("BitVec.ult", arguments, environment),
        "bvule" => comparison("BitVec.ule", arguments, environment),
        "bvugt" => reversed_comparison("BitVec.ult", arguments, environment),
        "bvuge" => reversed_comparison("BitVec.ule", arguments, environment),
        "bvslt" => comparison("BitVec.slt", arguments, environment),
        "bvsle" => comparison("BitVec.sle", arguments, environment),
        "bvsgt" => reversed_comparison("BitVec.slt", arguments, environment),
        "bvsge" => reversed_comparison("BitVec.sle", arguments, environment),
        other => Err(format!("unsupported SMT operator {other:?}")),
    }
}

fn render_indexed_literal(arguments: &[SExpr]) -> Result<Rendered, String> {
    if arguments.len() != 2 {
        return Err("malformed indexed literal".into());
    }
    let literal = arguments[0].atom("indexed literal")?;
    let value = literal
        .strip_prefix("bv")
        .ok_or_else(|| "unsupported indexed expression".to_string())?
        .parse::<u128>()
        .map_err(|_| "bit-vector literal exceeds 128 bits".to_string())?;
    let width = parse_usize(&arguments[1], "bit-vector width")?;
    if width == 0 || width > 128 {
        return Err("bit-vector literal width must be in 1..=128".into());
    }
    if width < 128 && value >= (1_u128 << width) {
        return Err("bit-vector literal does not fit its width".into());
    }
    Ok(bitvec_literal(value, width))
}

fn render_indexed(
    indexed: &[SExpr],
    arguments: &[SExpr],
    environment: &BTreeMap<String, Binding>,
) -> Result<Rendered, String> {
    if indexed.is_empty() || indexed[0].atom("indexed operator marker")? != "_" {
        return Err("malformed indexed operator".into());
    }
    let operator = indexed
        .get(1)
        .ok_or_else(|| "missing indexed operator".to_string())?
        .atom("indexed operator")?;
    match operator {
        "extract" => {
            if indexed.len() != 4 || arguments.len() != 1 {
                return Err("malformed extract".into());
            }
            let high = parse_usize(&indexed[2], "extract high")?;
            let low = parse_usize(&indexed[3], "extract low")?;
            let value = render(&arguments[0], environment)?;
            let Sort::BitVec(width) = value.sort else {
                return Err("extract operand is not a bit-vector".into());
            };
            if low > high || high >= width {
                return Err("extract indices are out of range".into());
            }
            Ok(Rendered {
                sort: Sort::BitVec(high - low + 1),
                text: format!("BitVec.extractLsb {high} {low} {}", value.text),
            })
        }
        "zero_extend" | "sign_extend" => {
            if indexed.len() != 3 || arguments.len() != 1 {
                return Err(format!("malformed {operator}"));
            }
            let amount = parse_usize(&indexed[2], "extension amount")?;
            let value = render(&arguments[0], environment)?;
            let Sort::BitVec(width) = value.sort else {
                return Err("extension operand is not a bit-vector".into());
            };
            let target = width
                .checked_add(amount)
                .ok_or_else(|| "extended width overflow".to_string())?;
            let function = if operator == "zero_extend" {
                "BitVec.zeroExtend"
            } else {
                "BitVec.signExtend"
            };
            Ok(Rendered {
                sort: Sort::BitVec(target),
                text: format!("{function} {target} {}", value.text),
            })
        }
        "rotate_left" | "rotate_right" => {
            if indexed.len() != 3 || arguments.len() != 1 {
                return Err(format!("malformed {operator}"));
            }
            let amount = parse_usize(&indexed[2], "rotation amount")?;
            let value = render(&arguments[0], environment)?;
            let Sort::BitVec(_) = value.sort else {
                return Err("rotation operand is not a bit-vector".into());
            };
            let function = if operator == "rotate_left" {
                "BitVec.rotateLeft"
            } else {
                "BitVec.rotateRight"
            };
            Ok(Rendered {
                sort: value.sort,
                text: format!("{function} {} {amount}", value.text),
            })
        }
        other => Err(format!("unsupported indexed SMT operator {other:?}")),
    }
}

fn unary_bool(
    operator: &str,
    arguments: &[SExpr],
    environment: &BTreeMap<String, Binding>,
) -> Result<Rendered, String> {
    require_arity(arguments, 1, "Boolean unary operator")?;
    let value = expect_sort(render(&arguments[0], environment)?, Sort::Bool)?;
    Ok(Rendered {
        sort: Sort::Bool,
        text: format!("{operator}({})", value.text),
    })
}

fn nary_bool(
    operator: &str,
    identity: &str,
    arguments: &[SExpr],
    environment: &BTreeMap<String, Binding>,
) -> Result<Rendered, String> {
    let mut values = Vec::with_capacity(arguments.len());
    for argument in arguments {
        values.push(expect_sort(render(argument, environment)?, Sort::Bool)?.text);
    }
    Ok(Rendered {
        sort: Sort::Bool,
        text: values
            .into_iter()
            .reduce(|left, right| format!("({left} {operator} {right})"))
            .unwrap_or_else(|| identity.into()),
    })
}

fn nary_xor(
    arguments: &[SExpr],
    environment: &BTreeMap<String, Binding>,
) -> Result<Rendered, String> {
    if arguments.is_empty() {
        return Err("xor requires at least one argument".into());
    }
    let mut values = Vec::with_capacity(arguments.len());
    for argument in arguments {
        values.push(expect_sort(render(argument, environment)?, Sort::Bool)?.text);
    }
    Ok(Rendered {
        sort: Sort::Bool,
        text: values
            .into_iter()
            .reduce(|left, right| format!("decide ({left} != {right})"))
            .expect("nonempty xor"),
    })
}

fn implication(
    arguments: &[SExpr],
    environment: &BTreeMap<String, Binding>,
) -> Result<Rendered, String> {
    if arguments.len() < 2 {
        return Err("implication requires at least two arguments".into());
    }
    let mut values = Vec::with_capacity(arguments.len());
    for argument in arguments {
        values.push(expect_sort(render(argument, environment)?, Sort::Bool)?.text);
    }
    let mut result = values.pop().expect("at least two implication operands");
    while let Some(value) = values.pop() {
        result = format!("(!({value}) || {result})");
    }
    Ok(Rendered {
        sort: Sort::Bool,
        text: result,
    })
}

fn equality(
    arguments: &[SExpr],
    environment: &BTreeMap<String, Binding>,
    distinct: bool,
) -> Result<Rendered, String> {
    if arguments.len() < 2 {
        return Err("equality/distinct requires at least two arguments".into());
    }
    let values = arguments
        .iter()
        .map(|argument| render(argument, environment))
        .collect::<Result<Vec<_>, _>>()?;
    let sort = values[0].sort;
    if values.iter().any(|value| value.sort != sort) {
        return Err("equality operands have different sorts".into());
    }
    let comparison = if distinct { "!=" } else { "=" };
    let pairs = if distinct {
        let mut pairs = Vec::new();
        for left in 0..values.len() {
            for right in (left + 1)..values.len() {
                pairs.push(format!(
                    "decide ({} {comparison} {})",
                    values[left].text, values[right].text
                ));
            }
        }
        pairs
    } else {
        values
            .windows(2)
            .map(|pair| format!("decide ({} {comparison} {})", pair[0].text, pair[1].text))
            .collect()
    };
    Ok(Rendered {
        sort: Sort::Bool,
        text: pairs
            .into_iter()
            .reduce(|left, right| format!("({left} && {right})"))
            .expect("at least one equality pair"),
    })
}

fn ite(arguments: &[SExpr], environment: &BTreeMap<String, Binding>) -> Result<Rendered, String> {
    require_arity(arguments, 3, "ite")?;
    let condition = expect_sort(render(&arguments[0], environment)?, Sort::Bool)?;
    let then_value = render(&arguments[1], environment)?;
    let else_value = render(&arguments[2], environment)?;
    if then_value.sort != else_value.sort {
        return Err("ite branches have different sorts".into());
    }
    Ok(Rendered {
        sort: then_value.sort,
        text: format!(
            "if {} = true then {} else {}",
            condition.text, then_value.text, else_value.text
        ),
    })
}

fn unary_bv(
    operator: &str,
    arguments: &[SExpr],
    environment: &BTreeMap<String, Binding>,
) -> Result<Rendered, String> {
    require_arity(arguments, 1, "bit-vector unary operator")?;
    let value = render(&arguments[0], environment)?;
    let Sort::BitVec(_) = value.sort else {
        return Err("bit-vector unary operand has the wrong sort".into());
    };
    Ok(Rendered {
        sort: value.sort,
        text: format!("{operator}({})", value.text),
    })
}

fn binary_bv(
    operator: &str,
    arguments: &[SExpr],
    environment: &BTreeMap<String, Binding>,
) -> Result<Rendered, String> {
    binary_bv_with(arguments, environment, |left, right| {
        format!("({left} {operator} {right})")
    })
}

fn binary_bv_call(
    function: &str,
    arguments: &[SExpr],
    environment: &BTreeMap<String, Binding>,
) -> Result<Rendered, String> {
    binary_bv_with(arguments, environment, |left, right| {
        format!("{function} {left} {right}")
    })
}

fn shift_bv(
    operator: &str,
    arguments: &[SExpr],
    environment: &BTreeMap<String, Binding>,
) -> Result<Rendered, String> {
    binary_bv_with(arguments, environment, |left, right| {
        format!("({left} {operator} {right}.toNat)")
    })
}

fn binary_bv_with(
    arguments: &[SExpr],
    environment: &BTreeMap<String, Binding>,
    operation: impl FnOnce(&str, &str) -> String,
) -> Result<Rendered, String> {
    require_arity(arguments, 2, "bit-vector binary operator")?;
    let left = render(&arguments[0], environment)?;
    let right = render(&arguments[1], environment)?;
    if left.sort != right.sort || !matches!(left.sort, Sort::BitVec(_)) {
        return Err("bit-vector binary operands have different or invalid sorts".into());
    }
    Ok(Rendered {
        sort: left.sort,
        text: operation(&left.text, &right.text),
    })
}

fn concat(
    arguments: &[SExpr],
    environment: &BTreeMap<String, Binding>,
) -> Result<Rendered, String> {
    require_arity(arguments, 2, "concat")?;
    let left = render(&arguments[0], environment)?;
    let right = render(&arguments[1], environment)?;
    let (Sort::BitVec(left_width), Sort::BitVec(right_width)) = (left.sort, right.sort) else {
        return Err("concat operands are not bit-vectors".into());
    };
    Ok(Rendered {
        sort: Sort::BitVec(
            left_width
                .checked_add(right_width)
                .ok_or_else(|| "concat width overflow".to_string())?,
        ),
        text: format!("BitVec.append {} {}", left.text, right.text),
    })
}

fn comparison(
    function: &str,
    arguments: &[SExpr],
    environment: &BTreeMap<String, Binding>,
) -> Result<Rendered, String> {
    require_arity(arguments, 2, "bit-vector comparison")?;
    let left = render(&arguments[0], environment)?;
    let right = render(&arguments[1], environment)?;
    if left.sort != right.sort || !matches!(left.sort, Sort::BitVec(_)) {
        return Err("bit-vector comparison operands have different or invalid sorts".into());
    }
    Ok(Rendered {
        sort: Sort::Bool,
        text: format!("{function} {} {}", left.text, right.text),
    })
}

fn reversed_comparison(
    function: &str,
    arguments: &[SExpr],
    environment: &BTreeMap<String, Binding>,
) -> Result<Rendered, String> {
    require_arity(arguments, 2, "bit-vector comparison")?;
    comparison(
        function,
        &[arguments[1].clone(), arguments[0].clone()],
        environment,
    )
}

fn expect_sort(value: Rendered, expected: Sort) -> Result<Rendered, String> {
    if value.sort == expected {
        Ok(value)
    } else {
        Err(format!(
            "sort mismatch: expected {expected:?}, found {:?}",
            value.sort
        ))
    }
}

fn require_arity(arguments: &[SExpr], expected: usize, operator: &str) -> Result<(), String> {
    if arguments.len() == expected {
        Ok(())
    } else {
        Err(format!(
            "{operator} expects {expected} operands, found {}",
            arguments.len()
        ))
    }
}

fn bitvec_literal(value: u128, width: usize) -> Rendered {
    Rendered {
        sort: Sort::BitVec(width),
        text: format!("BitVec.ofNat {width} {value}"),
    }
}

fn parse_sort(expression: &SExpr) -> Result<Sort, String> {
    match expression {
        SExpr::Atom(atom) if atom == "Bool" => Ok(Sort::Bool),
        SExpr::List(items) if items.len() == 3 => {
            if items[0].atom("sort marker")? != "_" || items[1].atom("sort name")? != "BitVec" {
                return Err("arrays and non-bit-vector sorts are unsupported".into());
            }
            let width = parse_usize(&items[2], "bit-vector width")?;
            if width == 0 || width > 128 {
                return Err("bit-vector width must be in 1..=128".into());
            }
            Ok(Sort::BitVec(width))
        }
        _ => Err("arrays and non-bit-vector sorts are unsupported".into()),
    }
}

fn parse_usize(expression: &SExpr, context: &str) -> Result<usize, String> {
    expression
        .atom(context)?
        .parse()
        .map_err(|_| format!("invalid {context}"))
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum SExpr {
    Atom(String),
    List(Vec<Self>),
}

impl SExpr {
    fn atom<'a>(&'a self, context: &str) -> Result<&'a str, String> {
        match self {
            Self::Atom(atom) => Ok(atom),
            Self::List(_) => Err(format!("expected atom for {context}")),
        }
    }

    fn list<'a>(&'a self, context: &str) -> Result<&'a [Self], String> {
        match self {
            Self::List(items) => Ok(items),
            Self::Atom(_) => Err(format!("expected list for {context}")),
        }
    }
}

fn parse_one(source: &str) -> Result<SExpr, String> {
    let mut expressions = parse_many(source)?;
    if expressions.len() != 1 {
        return Err("expected exactly one SMT expression".into());
    }
    Ok(expressions.remove(0))
}

fn parse_many(source: &str) -> Result<Vec<SExpr>, String> {
    if !source.is_ascii() || source.contains([';', '"', '\\']) {
        return Err("comments, strings, escapes, and non-ASCII SMT are unsupported".into());
    }
    let tokens = tokenize(source)?;
    let mut cursor = 0;
    let mut expressions = Vec::new();
    while cursor < tokens.len() {
        expressions.push(parse_expression(&tokens, &mut cursor)?);
    }
    Ok(expressions)
}

fn tokenize(source: &str) -> Result<Vec<String>, String> {
    let bytes = source.as_bytes();
    let mut tokens = Vec::new();
    let mut cursor = 0;
    while cursor < bytes.len() {
        if bytes[cursor].is_ascii_whitespace() {
            cursor += 1;
        } else if matches!(bytes[cursor], b'(' | b')') {
            tokens.push((bytes[cursor] as char).to_string());
            cursor += 1;
        } else if bytes[cursor] == b'|' {
            let start = cursor;
            cursor += 1;
            while cursor < bytes.len() && bytes[cursor] != b'|' {
                if matches!(bytes[cursor], b'(' | b')') || bytes[cursor].is_ascii_control() {
                    return Err("invalid quoted SMT symbol".into());
                }
                cursor += 1;
            }
            if cursor == bytes.len() {
                return Err("unterminated quoted SMT symbol".into());
            }
            cursor += 1;
            tokens.push(source[start..cursor].into());
        } else {
            let start = cursor;
            while cursor < bytes.len()
                && !bytes[cursor].is_ascii_whitespace()
                && !matches!(bytes[cursor], b'(' | b')')
            {
                if bytes[cursor].is_ascii_control() {
                    return Err("control character in SMT token".into());
                }
                cursor += 1;
            }
            tokens.push(source[start..cursor].into());
        }
    }
    Ok(tokens)
}

fn parse_expression(tokens: &[String], cursor: &mut usize) -> Result<SExpr, String> {
    let token = tokens
        .get(*cursor)
        .ok_or_else(|| "unexpected end of SMT expression".to_string())?;
    *cursor += 1;
    match token.as_str() {
        "(" => {
            let mut items = Vec::new();
            while tokens.get(*cursor).is_some_and(|token| token != ")") {
                items.push(parse_expression(tokens, cursor)?);
            }
            if tokens.get(*cursor).map(String::as_str) != Some(")") {
                return Err("unclosed SMT list".into());
            }
            *cursor += 1;
            Ok(SExpr::List(items))
        }
        ")" => Err("unexpected closing parenthesis".into()),
        atom => Ok(SExpr::Atom(atom.into())),
    }
}

fn validate_namespace(namespace: &str) -> Result<(), SymbolicLeanError> {
    let valid = !namespace.is_empty()
        && namespace.split('.').all(|part| {
            let mut characters = part.chars();
            characters
                .next()
                .is_some_and(|first| first.is_ascii_uppercase())
                && characters.all(|character| character.is_ascii_alphanumeric() || character == '_')
        });
    if valid {
        Ok(())
    } else {
        Err(SymbolicLeanError::InvalidNamespace(namespace.into()))
    }
}

fn is_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::VC_SCHEMA_VERSION;

    fn symbolic_vc(declarations: &str, formula: &str) -> VcArtifact {
        VcArtifact {
            kind: VcKind::InitialSafety,
            declarations: declarations.into(),
            formula: formula.into(),
            product_hash: "a".repeat(64),
            certificate_hash: "b".repeat(64),
            schema_version: VC_SCHEMA_VERSION,
        }
    }

    fn checker() -> LeanCheckerFingerprint {
        LeanCheckerFingerprint {
            lean_version: "Lean 4.22.0".into(),
            lake_version: "Lake 5.0.0".into(),
            lean_binary_blake3: "1".repeat(64),
            lake_binary_blake3: "2".repeat(64),
            lean_toolchain_blake3: "3".repeat(64),
            lake_manifest_blake3: "4".repeat(64),
        }
    }

    #[test]
    fn renders_free_bitvectors_as_symbolic_bv_decide_theorem() {
        let vc = symbolic_vc(
            "(declare-fun s () (_ BitVec 4))\n(define-fun stable () Bool (= (bvadd s (_ bv1 4)) (bvadd s (_ bv1 4))))",
            "(=> stable (not (distinct s s)))",
        );
        let module =
            export_symbolic_vc_module("Pceq.Generated.Symbolic", std::slice::from_ref(&vc))
                .expect("symbolic export");
        let digest = vc.digest();
        let problem_digest = blake3::hash(vc.smt2_validity_query().as_bytes())
            .to_hex()
            .to_string();
        assert!(module.source.contains("(v0 : BitVec 4)"));
        assert!(module.source.contains("let d0 : Bool :="));
        assert!(module.source.contains("bv_decide"));
        assert!(!module.source.contains("native_decide"));
        assert!(module.source.contains(&format!("\"{digest}\"")));
        assert!(module.source.contains(&format!("\"{problem_digest}\"")));
        assert!(module.source.contains(&format!("\"{}\"", vc.product_hash)));
        assert!(module
            .source
            .contains(&format!("\"{}\"", vc.certificate_hash)));
        assert_eq!(module.manifest[0].checker, SYMBOLIC_LEAN_CHECKER);
        module.validate_exact(&[vc]).expect("exact rerender");
    }

    #[test]
    fn accepts_unroll_style_quoted_symbols_and_zero_arity_definitions() {
        let vc = symbolic_vc(
            "(declare-fun |pceq.state.3@0| () (_ BitVec 1))\n(define-fun |pceq.value.2@0| () (_ BitVec 1) #b0)",
            "(=> (= |pceq.state.3@0| |pceq.value.2@0|) (not (= |pceq.value.2@0| #b1)))",
        );
        let module = export_symbolic_vc_module("Pceq.Generated.Symbolic", &[vc])
            .expect("unroll-style export");
        assert!(module.source.contains("(v0 : BitVec 1)"));
        assert!(module
            .source
            .contains("let d0 : BitVec 1 := BitVec.ofNat 1 0"));
    }

    #[test]
    fn renders_symbolic_safety_consecution() {
        let mut vc = symbolic_vc(
            "(declare-const s (_ BitVec 4))\n(declare-const s_next (_ BitVec 4))",
            "(=> (and (= s s) (= s_next (bvadd s (_ bv0 4)))) (= s_next s))",
        );
        vc.kind = VcKind::EdgeConsecution;
        let module = export_symbolic_vc_module(
            "Pceq.Generated.SymbolicConsecution",
            std::slice::from_ref(&vc),
        )
        .expect("consecution export");
        assert!(module.source.contains("(v0 : BitVec 4)"));
        assert!(module.source.contains("(v1 : BitVec 4)"));
        assert_eq!(module.manifest[0].kind, VcKind::EdgeConsecution);
    }

    #[test]
    fn exact_source_manifest_and_linkage_tamper_fail_closed() {
        let vc = symbolic_vc(
            "(declare-const x (_ BitVec 8))",
            "(= (bvadd x (_ bv1 8)) (bvadd x (_ bv1 8)))",
        );
        let module =
            export_symbolic_vc_module("Pceq.Generated.Symbolic", std::slice::from_ref(&vc))
                .expect("symbolic export");
        let artifact =
            SymbolicLeanAuditArtifact::new(checker(), module.clone(), std::slice::from_ref(&vc))
                .expect("exact artifact");
        artifact
            .validate_exact(std::slice::from_ref(&vc))
            .expect("valid artifact");

        let mut source_tamper = module.clone();
        source_tamper.source.push(' ');
        assert_eq!(
            source_tamper.validate_exact(std::slice::from_ref(&vc)),
            Err(SymbolicLeanError::ExactRerender)
        );

        let mut manifest_tamper = module;
        manifest_tamper.manifest[0].problem_digest = "f".repeat(64);
        assert_eq!(
            manifest_tamper.validate_exact(std::slice::from_ref(&vc)),
            Err(SymbolicLeanError::ExactRerender)
        );

        let mut vc_tamper = vc;
        vc_tamper.certificate_hash = "c".repeat(64);
        assert_eq!(
            artifact.validate_exact(&[vc_tamper]),
            Err(SymbolicLeanError::ExactRerender)
        );
    }

    #[test]
    fn pceq_smt_unroll_initial_safety_reaches_symbolic_export() {
        use std::str::FromStr;

        use pceq_btor2::BtorProgram;
        use pceq_smt::{unroll, SmtLogic};

        let source = include_str!("../../../fixtures/btor2/toy_counter.btor2");
        let program = BtorProgram::from_str(source).expect("parse BTOR2 fixture");
        let system = unroll(&program).expect("unroll fixture");
        assert_eq!(system.logic, SmtLogic::QfBv);
        let vc = VcArtifact {
            kind: VcKind::InitialSafety,
            declarations: system.declarations_through(0),
            formula: system.initial_safety_formula(),
            product_hash: system.source_hash.clone(),
            certificate_hash: "c".repeat(64),
            schema_version: VC_SCHEMA_VERSION,
        };
        let module = export_symbolic_vc_module(
            "Pceq.Generated.ToyCounterInitialSafety",
            std::slice::from_ref(&vc),
        )
        .expect("export unrolled VC");
        assert!(module.source.contains("(v0 : BitVec 1)"));
        assert!(module.source.contains("bv_decide"));
        assert_eq!(module.manifest[0].product_hash, system.source_hash);
        module
            .validate_exact(std::slice::from_ref(&vc))
            .expect("exact unrolled rerender");
    }

    #[test]
    fn rejects_arrays_quantifiers_unsupported_kinds_and_command_injection() {
        let cases = [
            symbolic_vc(
                "(declare-fun memory () (Array (_ BitVec 4) (_ BitVec 8)))",
                "true",
            ),
            symbolic_vc(
                "(declare-fun x () (_ BitVec 4))",
                "(forall ((y (_ BitVec 4))) (= x y))",
            ),
        ];
        for vc in cases {
            assert!(matches!(
                export_symbolic_vc_module("Pceq.Generated.Symbolic", &[vc]),
                Err(SymbolicLeanError::UnsupportedSmt { .. })
            ));
        }

        let mut wrong_kind = symbolic_vc("(declare-const x (_ BitVec 4))", "(= x x)");
        wrong_kind.kind = VcKind::InputCompleteness;
        assert!(matches!(
            export_symbolic_vc_module("Pceq.Generated.Symbolic", &[wrong_kind]),
            Err(SymbolicLeanError::UnsupportedKind { .. })
        ));

        let injected_declaration =
            symbolic_vc("(declare-const x (_ BitVec 4))\n(assert (= x x))", "true");
        assert!(matches!(
            export_symbolic_vc_module("Pceq.Generated.Symbolic", &[injected_declaration]),
            Err(SymbolicLeanError::InvalidArtifact { .. })
        ));

        let injected_term = symbolic_vc(
            "(declare-const x (_ BitVec 4))",
            "(and (= x x) (check-sat))",
        );
        assert!(matches!(
            export_symbolic_vc_module("Pceq.Generated.Symbolic", &[injected_term]),
            Err(SymbolicLeanError::InvalidArtifact { .. })
        ));
    }
}
