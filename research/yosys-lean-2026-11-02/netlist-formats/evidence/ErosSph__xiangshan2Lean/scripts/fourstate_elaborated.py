"""Import pinned slang's elaborated hierarchy into the independent four-state IR.

slang evaluates parameters, creates generate scopes and binds port conversions.
This module imports those results; it does not implement an RTL elaborator.
"""
from __future__ import annotations

from pathlib import Path
import os
import re

from bootstrap_fourstate import load_parser

SIMPLE = re.compile(r'[A-Za-z_][A-Za-z0-9_$]*\Z')
BINARY = {'Add': 'add', 'Subtract': 'sub', 'BinaryAnd': 'and', 'BinaryOr': 'or',
          'BinaryXor': 'xor', 'BinaryXnor': 'xnor', 'Equality': 'eq', 'Inequality': 'ne',
          'CaseEquality': 'case_eq', 'CaseInequality': 'case_ne', 'LessThan': 'lt',
          'LessThanEqual': 'le', 'GreaterThan': 'gt', 'GreaterThanEqual': 'ge',
          'LogicalAnd': 'logical_and', 'LogicalOr': 'logical_or',
          'LogicalShiftLeft': 'shl', 'LogicalShiftRight': 'shr',
          'ArithmeticShiftLeft': 'ashl', 'ArithmeticShiftRight': 'ashr'}
UNARY = {'Plus': 'pos', 'Minus': 'neg', 'BitwiseNot': 'not', 'LogicalNot': 'logical_not',
         'BitwiseAnd': 'reduce_and', 'BitwiseOr': 'reduce_or'}


def source_files(source):
    source = Path(source).resolve()
    files = sorted((p.resolve() for p in source.rglob('*') if p.is_file() and p.suffix.lower() in ('.sv', '.v')),
                   key=lambda p: p.as_posix()) if source.is_dir() else [source]
    if not files or any(not p.is_file() for p in files):
        raise ValueError('RTL source file or nonempty .sv/.v directory required: ' + str(source))
    return files


def parse(source, top, mode='combinational', clock=None, *, defines=(), include_dirs=(), preprocessing=None, async_reset=None, bounded_memories=False, bounded_events=False, event_clocks=(), immediate_assertions=False, _compact=False, _scalable=False, _eventful=False, _extensions=False):
    if mode not in ('combinational', 'sequential') or (mode == 'sequential') != bool(clock):
        raise ValueError('Sequential mode requires --clock; combinational mode forbids it')
    if _compact and (not bounded_memories or not bounded_events):
        raise ValueError('Private compact import requires memory and event contracts')
    if _scalable and not _compact:
        raise ValueError('Private scalable import requires compact state')
    if _eventful and not _scalable:
        raise ValueError('Private eventful import requires scalable state')
    if (event_clocks and not bounded_events) or (bounded_events and (mode != 'sequential' or (bounded_memories and not _compact) or async_reset is not None)):
        raise ValueError('bounded events require sequential mode, no memory opt-in and no legacy async-reset option')
    roots = tuple(event_clocks) or ((clock,) if bounded_events else ())
    if bounded_events and (not 1 <= len(roots) <= 2 or len(set(roots)) != len(roots) or clock not in roots):
        raise ValueError('bounded events require one or two distinct root clocks including --clock')
    if immediate_assertions and not bounded_events:
        raise ValueError('immediate assertions require explicit bounded-events mode')
    from fourstate_preprocess import parse_sources, location
    pp = {} if preprocessing is None else preprocessing
    s, sm, trees, files = parse_sources(source, defines, include_dirs, pp)
    if async_reset is not None and (mode != 'sequential' or async_reset == clock):
        raise ValueError('Asynchronous reset requires sequential mode and a distinct --clock')
    def diagnostic_text(items):
        text = s.DiagnosticEngine.reportAll(sm, items)
        for file in files:
            try:
                relative = os.path.relpath(file)
            except ValueError:
                continue  # A different Windows drive is already printed absolute.
            text = text.replace(relative, str(file))
        return text
    for tree in trees:
        def unsupported_directive(n):
            if isinstance(n, s.syntax.SyntaxNode):
                k = str(n.kind).split('.')[-1]
                if k == 'BindDirective' or 'DefParam' in k:
                    p = location(sm, n.sourceRange.start)
                    raise ValueError(f"{p['file']}:{p['line']}:{p['column']}: {k} unsupported")
        tree.root.visit(unsupported_directive)
    options = s.ast.CompilationOptions()
    options.topModules = {top}
    options.maxInstanceDepth = 128
    options.maxGenerateSteps = 65536
    compilation = s.ast.Compilation(s.Bag([options]))
    for tree in trees:
        compilation.addSyntaxTree(tree)
    root = compilation.getRoot()
    diagnostics = compilation.getAllDiagnostics()
    if any(d.isError() for d in diagnostics):
        raise ValueError(diagnostic_text(diagnostics))
    if len(root.topInstances) != 1 or root.topInstances[0].name != top:
        raise ValueError(f'{files[0]}:1:1: --top must identify exactly one module ({top})')
    instance = root.topInstances[0]

    def kind(n):
        return str(n.kind).split('.')[-1]

    def loc(n):
        pos = n.sourceRange.start if hasattr(n, 'sourceRange') else n.location
        return location(sm, pos)

    def fail(n, message):
        p = loc(n)
        raise ValueError(f"{p['file']}:{p['line']}:{p['column']}: {message} ({kind(n)})")

    def simple(n):
        if not SIMPLE.fullmatch(n.name):
            fail(n, 'simple unescaped identifier required: ' + n.name)

    def bits(value, width=None):
        width = value.bitWidth if width is None else width
        result = ''.join(str(value[i]).lower() for i in reversed(range(value.bitWidth)))
        fill = result[0] if value.isSigned else '0'
        return (fill * width + result)[-width:]

    def constant_audit(e, local_values=(), known_parameters=False, context=None):
        def visit(n):
            if isinstance(n, s.ast.Expression):
                if (kind(n) == 'ConditionalOp' and n.type.isIntegral and len(n.conditions) == 1
                        and n.conditions[0].pattern is None):
                    # The pinned slang constant folder preserves matching Z
                    # bits, unlike IEEE Table 11-20. Do not silently import an
                    # incorrect parameter or elaborate a wrong generate branch.
                    def pure(part):
                        if (isinstance(part, s.ast.Expression) and
                            (kind(part) == 'Assignment' or (kind(part) == 'UnaryOp' and
                             str(part.op).split('.')[-1] in ('Preincrement','Postincrement','Predecrement','Postdecrement')))):
                            fail(part, 'side effects in structural conditional unsupported')
                    n.visit(pure)
                    ctx = context if context is not None else s.ast.EvalContext(instance.body)
                    c = n.conditions[0].expr.eval(ctx).value
                    a, b = n.left.eval(ctx).value, n.right.eval(ctx).value
                    value = n.eval(ctx).value
                    if all(isinstance(v, s.SVInt) for v in (c, a, b, value)):
                        condition = bits(c)
                        if '1' not in condition and set(condition) & set('xz'):
                            expected = ''.join(x if x == y and x in '01' else 'x'
                                               for x, y in zip(bits(a, n.type.bitWidth), bits(b, n.type.bitWidth)))
                            if bits(value, n.type.bitWidth) != expected:
                                fail(n, 'unsupported constant conditional folding: pinned pyslang differs from IEEE 1800 Table 11-20')
                if kind(n) == 'Call' and (not n.isSystemCall or n.subroutineName not in ('$clog2', '$bits', '$signed', '$unsigned')):
                    fail(n, 'constant function unsupported')
                if kind(n) == 'Call' and n.isSystemCall and n.subroutineName == '$bits':
                    return s.ast.VisitAction.Skip
                if kind(n) in ('NamedValue', 'HierarchicalValue'):
                    if kind(n.symbol) not in ('Parameter', 'Genvar') and n.symbol not in local_values:
                        fail(n, 'constant expression references hardware')
                    if known_parameters and kind(n.symbol) == 'Parameter':
                        value = n.symbol.value.value
                        if not isinstance(value, s.SVInt) or value.hasUnknown:
                            fail(n, 'fully known integral generate-for parameter required')
        if e is not None:
            e.visit(visit)

    def integer(e, scope):
        constant_audit(e)
        context = (s.ast.EvalContext(scope) if isinstance(scope, s.ast.Symbol) else
                   s.ast.EvalContext(s.ast.ASTContext(scope, s.ast.LookupLocation.max)))
        value = e.eval(context).value
        if not isinstance(value, s.SVInt) or value.hasUnknown:
            fail(e, 'fully known integral structural expression required')
        return int(value)

    def audit_generate_for(n, scope):
        # Audit official expressions at official iteration values. Do not decide
        # which scopes exist or reproduce slang's generate expansion algorithm.
        variable = n.loopVariable
        for e in (n.initialExpression, n.stopExpression, n.iterExpression):
            constant_audit(e, (variable,), known_parameters=True)

        def evaluate(e, context):
            constant_audit(e, (variable,), known_parameters=True, context=context)
            value = e.eval(context)
            if not isinstance(value.value, s.SVInt) or value.value.hasUnknown:
                fail(e, 'fully known integral generate-for structural expression required')
            return value

        initial = evaluate(n.initialExpression, s.ast.EvalContext(scope))
        values = [initial] + [entry.find(variable.name).value for entry in n.entries]
        for value in values:
            context = s.ast.EvalContext(scope)
            context.createLocal(variable, value)
            evaluate(n.stopExpression, context)
            evaluate(n.iterExpression, context)
            # The evaluator applies the increment to its local, including ++,
            # -- and compound assignment. Check the terminal condition too.
            current = context.findLocal(variable).value
            if not isinstance(current, s.SVInt) or current.hasUnknown:
                fail(n.iterExpression, 'fully known integral generate-for step required')
            evaluate(n.stopExpression, context)

    signals, symbols, owners_of_signal, parameter_owners = {}, {}, {}, {}
    memories = {}
    memory_bits = 0
    capabilities = set()
    raw_drives, processes, connections = [], [], []
    hierarchy = {'top': top, 'parser': 'pyslang 12.0.0', 'instances': [], 'parameters': [], 'generate': [], 'connections': [],
                 'options': {'topModules': [top], 'maxInstanceDepth': 128, 'maxGenerateSteps': 65536},
                 'diagnostics': diagnostic_text(diagnostics)}

    def declaration(n, is_port=False, element_type=None, process_local=False):
        simple(n)
        typ = n.type if element_type is None else element_type
        multidimensional = typ.isPackedArray and typ.arrayElementType.isPackedArray
        if (not typ.isIntegral or not typ.isFourState or typ.bitWidth < 1 or typ.isUnpackedArray
                or (not typ.isSimpleBitVector and not multidimensional)):
            fail(n, 'fixed four-state packed vector required')
        if multidimensional:
            if is_port or (kind(n) != 'Net' and not process_local) or typ.arrayElementType.arrayElementType.isPackedArray:
                fail(n, 'two packed dimensions require an internal wire or automatic process local')
            capabilities.add('packed_vectors')
            if min(typ.arrayElementType.fixedRange.left, typ.arrayElementType.fixedRange.right) < 0:
                fail(n, 'nonnegative packed range required')
        if min(typ.fixedRange.left, typ.fixedRange.right) < 0:
            fail(n, 'nonnegative declaration range required')
        if n.initializer is not None and kind(n) != 'Net' and not process_local:
            fail(n, 'declaration initializer unsupported')
        if kind(n) == 'Net' and (n.isImplicit or n.netType.name != 'wire' or n.delay is not None):
            fail(n, 'explicit plain wire required')
        syntax = n.syntax
        while syntax is not None and kind(syntax) not in ('ModuleDeclaration', 'GenerateBlock', 'IfGenerate', 'LoopGenerate'):
            if getattr(syntax, 'attributes', None):
                fail(syntax, 'attributes unsupported')
            if getattr(syntax, 'strength', None) is not None or getattr(syntax, 'delay', None) is not None:
                fail(syntax, 'strength/delay unsupported')
            syntax = syntax.parent

    def module_check(inst):
        simple(inst)
        if not inst.isModule or inst.arrayPath:
            fail(inst, 'ordinary scalar module instance required')
        definition = inst.definition
        simple(definition)
        syntax = definition.syntax
        if syntax.header.ports is None or kind(syntax.header.ports) != 'AnsiPortList':
            fail(syntax, 'ANSI module ports required')
        if getattr(syntax, 'attributes', None):
            fail(syntax, 'module attributes unsupported')
        # Wildcards and omitted connections must not be filled silently.
        if inst is not instance:
            def check(n):
                if isinstance(n, s.syntax.SyntaxNode) and kind(n) == 'WildcardPortConnection':
                    fail(n, 'wildcard port connection unsupported')
            inst.syntax.visit(check)

    def collect(scope, owning_instance):
        nonlocal memory_bits
        port_directions = {}
        if kind(scope) == 'InstanceBody':
            for p in scope.portList:
                simple(p)
                if kind(p) != 'Port' or str(p.direction).split('.')[-1] not in ('In', 'Out') or not p.isAnsiPort:
                    fail(p, 'ordinary ANSI input/output required')
                if p.initializer is not None:
                    fail(p, 'port initializer unsupported')
                port_directions[p.internalSymbol.hierarchicalPath] = 'input' if str(p.direction).endswith('.In') else 'output'
        for n in scope:
            k = kind(n)
            if k in ('Net', 'Variable'):
                if n.type.isUnpackedArray and bounded_memories:
                    if (mode != 'sequential' or async_reset is not None or k != 'Variable' or
                            n.hierarchicalPath in port_directions or kind(n.type) != 'FixedSizeUnpackedArrayType'):
                        fail(n, 'bounded memory requires an internal fixed one-dimensional variable and binary single-clock mode')
                    typ = n.type.elementType
                    if not typ.isSimpleBitVector or typ.isUnpackedArray:
                        fail(n, 'bounded memory requires a simple packed four-state element')
                    declaration(n, element_type=typ)
                    r = n.type.fixedRange
                    if _compact:
                        if min(r.left,r.right) < 0:fail(n, 'nonnegative memory range required')
                        base = n.hierarchicalPath
                        info = dict(name=base,logical_path=base,rtl_path=base[len(top)+1:],direction='internal',
                            width=typ.bitWidth,signed=bool(typ.isSigned),left=typ.fixedRange.left,right=typ.fixedRange.right,
                            memory_object=True,source=loc(n))
                        signals[base],symbols[base],owners_of_signal[base] = info,n,owning_instance
                        memories[base] = dict(name=base,rtl_path=info['rtl_path'],left=r.left,right=r.right,
                            depth=abs(r.left-r.right)+1,width=typ.bitWidth,signed=bool(typ.isSigned),
                            element_left=typ.fixedRange.left,element_right=typ.fixedRange.right,source=loc(n))
                        capabilities.add('compact_memory')
                        continue
                    indices = list(range(min(r.left, r.right), max(r.left, r.right)+1))
                    if min(indices) < 0 or len(indices) > 32 or memory_bits + len(indices)*typ.bitWidth > 1024:
                        fail(n, 'bounded memory budget: nonnegative range, at most 32 words per array and 1024 total state bits')
                    memory_bits += len(indices)*typ.bitWidth
                    base = n.hierarchicalPath
                    owners_of_signal[base] = owning_instance
                    words = []
                    for i in indices:
                        path = f'{base}[{i}]';words.append(path)
                        info = dict(name=path, logical_path=path, rtl_path=path[len(top)+1:], direction='internal',
                                    width=typ.bitWidth, signed=bool(typ.isSigned), left=typ.fixedRange.left,
                                    right=typ.fixedRange.right, source=loc(n), memory=base, memory_index=i)
                        signals[path], symbols[path], owners_of_signal[path] = info, n, owning_instance
                    memories[base] = dict(name=base, rtl_path=base[len(top)+1:], left=r.left, right=r.right,
                                          indices=indices, words=words, width=typ.bitWidth, source=loc(n))
                    capabilities.add('unpacked_memory')
                    continue
                declaration(n, n.hierarchicalPath in port_directions)
                path = n.hierarchicalPath
                typ, r = n.type, n.type.fixedRange
                info = {'name': path, 'logical_path': path, 'rtl_path': path[len(top)+1:],
                        'direction': port_directions.get(path, 'internal') if owning_instance == top else 'internal',
                        'width': typ.bitWidth, 'signed': bool(typ.isSigned), 'left': r.left, 'right': r.right,
                        'source': loc(n)}
                if info['direction'] != 'internal':
                    info['port_name'] = n.name
                signals[path], symbols[path], owners_of_signal[path] = info, n, owning_instance
                if typ.isPackedArray and typ.arrayElementType.isPackedArray:
                    ranges, current = [], typ
                    while current.isPackedArray:
                        bounds = current.fixedRange
                        if min(bounds.left, bounds.right) < 0:
                            fail(n, 'nonnegative packed range required')
                        ranges.append({'left': bounds.left, 'right': bounds.right,
                                       'element_width': current.arrayElementType.bitWidth})
                        current = current.arrayElementType
                    info['packed_ranges'] = ranges
                if n.initializer is not None:
                    capabilities.add('net_initializer')
                    raw_drives.append((path, n.initializer, scope, owning_instance, None, n))
            elif k == 'Parameter':
                simple(n)
                constant_audit(n.initializer)
                value = n.value.value
                if not isinstance(value, s.SVInt):
                    fail(n, 'integral parameter/localparam required')
                parameter_owners[n.hierarchicalPath] = owning_instance
                hierarchy['parameters'].append({'path': n.hierarchicalPath, 'width': value.bitWidth,
                                                 'signed': bool(value.isSigned), 'bits': bits(value),
                                                 'local': n.isLocalParam, 'source': loc(n)})
            elif k == 'Instance':
                module_check(n)
                record_instance(n)
                connections.append((n, owning_instance))
                collect(n.body, n.hierarchicalPath)
            elif k == 'GenerateBlock':
                if n.name:
                    simple(n)
                hierarchy['generate'].append({'path': n.hierarchicalPath, 'active': not n.isUninstantiated,
                                              'kind': str(n.branchKind), 'source': loc(n)})
                if n.conditionExpression is not None:
                    integer(n.conditionExpression, scope)
                for item in n.caseItemExpressions:
                    integer(item, scope)
                if not n.isUninstantiated:
                    collect(n, owning_instance)
            elif k == 'GenerateBlockArray':
                simple(n)
                if not n.valid:
                    fail(n, 'invalid generate expansion')
                audit_generate_for(n, scope)
                hierarchy['generate'].append({'path': n.hierarchicalPath, 'kind': 'for',
                                              'entries': [e.hierarchicalPath for e in n.entries], 'source': loc(n)})
                for entry in n.entries:
                    collect(entry, owning_instance)
            elif k == 'ContinuousAssign':
                if n.delay is not None or any(v is not None for v in n.driveStrength):
                    fail(n, 'continuous delay/strength unsupported')
                raw_drives.append((n.assignment.left, n.assignment.right, scope, owning_instance, None, n))
            elif k == 'ProceduralBlock':
                if str(n.procedureKind).split('.')[-1] not in (('Always', 'AlwaysFF', 'AlwaysLatch') if bounded_events else ('Always', 'AlwaysFF')):
                    fail(n, 'initial/nonclocked process execution unsupported')
                processes.append((n, scope, owning_instance))
            elif k == 'StatementBlock':
                # Import declarations only through the checked procedural AST;
                # these scopes are never module signals or persistent state.
                continue
            elif k not in ('Port', 'Genvar', 'EmptyMember'):
                fail(n, 'unsupported active elaborated symbol')

    def record_instance(inst):
        params = []
        for p in inst.body.parameters:
            if kind(p) == 'Parameter' and p.isLocalParam:
                continue
            v = p.value.value if kind(p) == 'Parameter' else None
            if not isinstance(v, s.SVInt):
                fail(p, 'integral value parameter required')
            params.append({'name': p.name, 'width': v.bitWidth, 'signed': bool(v.isSigned), 'bits': bits(v),
                           'overridden': p.isOverridden, 'source': loc(p)})
        hierarchy['instances'].append({'path': inst.hierarchicalPath, 'module': inst.definition.name,
                                       'parameters': params, 'source': loc(inst), 'definition': loc(inst.definition)})

    module_check(instance)
    record_instance(instance)
    collect(instance.body, top)
    if not _extensions and not any(p['direction'] == 'output' for p in signals.values()):
        fail(instance, 'at least one top output required')

    def reference(e, placeholder=None):
        if kind(e) in ('NamedValue', 'HierarchicalValue') and kind(e.symbol) in ('Net', 'Variable'):
            return e.symbol.hierarchicalPath
        if kind(e) == 'EmptyArgument' and placeholder is not None:
            return placeholder
        return None

    def audit_reference_owner(e, owner, role):
        def visit(n):
            if isinstance(n, s.ast.Expression) and kind(n) in ('NamedValue', 'HierarchicalValue'):
                if kind(n.symbol) in ('Net', 'Variable'):
                    if owners_of_signal.get(n.symbol.hierarchicalPath) != owner:
                        fail(n, 'cross-module internal reference unsupported in ' + role)
                elif kind(n.symbol) == 'Parameter' and parameter_owners.get(n.symbol.hierarchicalPath) != owner:
                    fail(n, 'cross-module parameter reference unsupported in ' + role)
        e.visit(visit)

    for child, parent_owner in connections:
        for p in child.portConnections:
            port, actual = p.port, p.expression
            if actual is None or kind(actual) == 'EmptyArgument':
                if not _extensions:fail(child, 'every input/output must be connected: ' + port.name)
                formal = port.internalSymbol.hierarchicalPath
                direction = str(port.direction)
                hierarchy['connections'].append(dict(instance=child.hierarchicalPath,port=port.name,direction=direction,
                    formal=formal,destination=None,open=True,source=loc(child)))
                if direction.endswith('.In'):
                    # A floating wire reads Z; an unwritten variable reads X.
                    # Default-valued ports and non-wire net types remain rejected.
                    value='z' if kind(port.internalSymbol)=='Net' else 'x'
                    signals[formal]['floating_bits']=value*signals[formal]['width']
                continue
            formal = port.internalSymbol.hierarchicalPath
            if str(port.direction).endswith('.In'):
                raw_drives.append((formal, actual, child.parentScope, parent_owner, None, child))
                destination = formal
            else:
                if kind(actual) != 'Assignment' or reference(actual.left) is None:
                    fail(actual, 'output connection must target a whole signal')
                # slang's synthetic output assignment crosses the boundary on
                # purpose; only its source actual target belongs to the parent.
                audit_reference_owner(actual.left, parent_owner, 'output connection target')
                raw_drives.append((actual.left, actual.right, child.parentScope, child.hierarchicalPath, formal, child))
                destination = reference(actual.left)
            hierarchy['connections'].append({'instance': child.hierarchicalPath, 'port': port.name,
                                               'direction': str(port.direction), 'formal': formal,
                                               'destination': destination, 'source': loc(child)})

    drives = {}
    for left, right, scope, owner, placeholder, origin in raw_drives:
        dest = left if isinstance(left, str) else reference(left)
        if not isinstance(left, str) and placeholder is None:
            audit_reference_owner(left, owner, 'assignment target')
        audit_reference_owner(right, owner, 'assignment source')
        if dest not in signals or signals[dest]['direction'] == 'input':
            fail(origin, 'whole declared non-input signal assignment required')
        if dest in drives:
            fail(origin, 'multiple drivers: ' + dest)
        drives[dest] = (right, scope, owner, placeholder, origin)

    def alias(e, placeholder):
        while kind(e) == 'Conversion' and e.isImplicit and e.type.bitWidth == 1 and e.operand.type.bitWidth == 1:
            e = e.operand
        return reference(e, placeholder)

    # F5.1: identity edges come from bound symbols, never source spelling or
    # arbitrary IR simplification. Driver uniqueness was checked above.
    reset_aliases = {}
    input_formals = {c['formal'] for c in hierarchy['connections'] if c['direction'].endswith('.In')}
    if bounded_events:
        for dest, (right, _, _, placeholder, origin) in drives.items():
            source_name = alias(right, placeholder)
            if (source_name in signals and signals[dest]['width'] == signals[source_name]['width'] == 1
                    and (dest in input_formals or kind(symbols[dest]) == 'Net')
                    and right.type.isFourState):
                reset_aliases[dest] = dict(source=source_name,
                    connection='input_port' if dest in input_formals else 'local_net',
                    location=loc(origin))

    clock_signal, clock_signals = top + '.' + str(clock), set()
    if mode == 'sequential':
        if clock_signal not in signals or signals[clock_signal]['direction'] != 'input' or signals[clock_signal]['width'] != 1:
            fail(instance, '--clock must name a one-bit top input')
        if bounded_events:
            for name in roots:
                signal = signals.get(top + '.' + name)
                if signal is None or signal['direction'] != 'input' or signal['width'] != 1:
                    fail(instance, 'event root must name a one-bit top input: ' + name)
        else:
            clock_signals.add(clock_signal)
        changed = True
        while changed:
            changed = False
            for dest, (right, _, _, placeholder, _) in drives.items():
                if signals[dest]['width'] == 1 and alias(right, placeholder) in clock_signals and dest not in clock_signals:
                    clock_signals.add(dest); changed = True
        for name in clock_signals:
            if signals[name]['direction'] == 'output':
                fail(symbols[name], 'clock cannot be observed as data output')
        if not processes:
            fail(instance, 'sequential design must contain registers')
    elif processes:
        fail(processes[0][0], 'clocked process requires sequential mode')

    ir_nodes, state_owners, process_bodies = [], {}, []
    scalar_nba_bounds = {}

    def add(n, op, w, args=(), signed=False, **extra):
        i = len(ir_nodes)
        ir_nodes.append({'id': i, 'op': op, 'width': w, 'signed': signed, 'args': list(args), 'source': loc(n), **extra})
        return i

    def audit_runtime(n):
        if n is None:
            return
        from fourstate_frontend import ALLOWED
        k = kind(n)
        if k.endswith('Name'):
            return  # binding and static selector evaluation are checked below
        if k == 'MultipleConcatenationExpression':
            audit_runtime(n.concatenation)
            return  # The replication count uses the official structural evaluator.
        if k == 'CastExpression':
            if kind(n.left).endswith('Type'):
                fail(n, 'type cast unsupported; use an integral size')
            audit_runtime(n.right)
            return
        extra = {'AssignmentPatternExpression','SimpleAssignmentPattern','StructuredAssignmentPattern','ReplicatedAssignmentPattern','ImplicitStringLiteralExpression','ModExpression','UnaryBitwiseXorExpression','UnaryBitwiseXnorExpression','UnbasedUnsizedLiteralExpression'} if _extensions else set()
        if k not in ALLOWED | {'UnaryBitwiseAndExpression', 'UnaryBitwiseOrExpression'} | extra:
            fail(n, 'unsupported hardware syntax')
        if k == 'IntegerLiteralExpression' and not _extensions:
            fail(n, 'unsized data literal unsupported')
        for child in n:
            if isinstance(child, s.syntax.SyntaxNode):
                audit_runtime(child)

    local_symbols = {}

    def expression(e, scope, owner, placeholder=None, local_values=None):
        k, w, signed = kind(e), e.type.bitWidth, bool(e.type.isSigned)
        def child(value):
            return expression(value, scope, owner, placeholder, local_values)
        if w < 1:
            fail(e, 'positive bit-vector expression required')
        if k in ('NamedValue', 'HierarchicalValue'):
            symbol = e.symbol
            if symbol in local_symbols:
                if k != 'NamedValue':
                    fail(e, 'hierarchical process local reference unsupported')
                if local_values is None or symbol not in local_values:
                    fail(e, 'process local read outside its active scope')
                return local_values[symbol]
            if kind(symbol) == 'Parameter':
                if parameter_owners.get(symbol.hierarchicalPath) != owner:
                    fail(e, 'cross-module parameter reference unsupported')
                return add(e, 'const', w, signed=signed, bits=bits(symbol.value.value, w))
            name = reference(e)
            if name not in signals or owners_of_signal[name] != owner:
                fail(e, 'cross-module internal reference unsupported')
            if name in clock_signals:
                fail(e, 'clock cannot be used as data')
            return add(e, 'ref', w, signed=signed, name=name)
        if k == 'EmptyArgument' and placeholder is not None:
            return add(e, 'ref', w, signed=signed, name=placeholder)
        if k == 'IntegerLiteral':
            if e.isDeclaredUnsized and not _extensions:
                fail(e, 'unsized data literal unsupported')
            return add(e, 'const', w, signed=signed, bits=bits(e.value, w))
        if _extensions and k=='UnbasedUnsizedIntegerLiteral':
            value=e.eval(s.ast.EvalContext(scope)).value
            if not isinstance(value,s.SVInt):fail(e,'bound integral unbased literal required')
            return add(e,'const',w,signed=signed,bits=str(value[0]).lower()*w)
        if k == 'Conversion':
            arg = child(e.operand)
            old = ir_nodes[arg]
            op = 'resize' if old['width'] != w else 'cast_sign'
            # Propagated operator context sets signedness before widening.
            # Assignment / explicit conversions instead extend the source type.
            extend_signed = signed if e.conversionKind == s.ast.ConversionKind.Propagated else bool(e.operand.type.isSigned)
            return add(e, op, w, [arg], signed=signed, from_width=old['width'],
                       extension='sign' if extend_signed else 'zero')
        if k == 'UnaryOp':
            op = UNARY.get(str(e.op).split('.')[-1])
            if _extensions:op={'BitwiseXor':'reduce_xor','BitwiseXnor':'reduce_xnor'}.get(str(e.op).split('.')[-1],op)
            if op is None:
                fail(e, 'unsupported unary hardware operator')
            if op.startswith('reduce_'):
                capabilities.add('reductions')
            return add(e, op, w, [child(e.operand)], signed=signed)
        if k == 'BinaryOp':
            op = BINARY.get(str(e.op).split('.')[-1])
            if _extensions and str(e.op).split('.')[-1]=='Mod':op='mod'
            if op is None:
                fail(e, 'unsupported binary hardware operator')
            args = [child(e.left), child(e.right)]
            return add(e, op, w, args, signed=signed,
                       compare_signed=bool(e.left.type.isSigned and e.right.type.isSigned), operand_width=e.left.type.bitWidth)
        if k == 'ConditionalOp':
            if len(e.conditions) != 1 or e.conditions[0].pattern is not None or (e.conditions[0].expr.type.bitWidth != 1 and not _extensions):
                fail(e, 'one-bit expression condition required')
            args = [child(e.conditions[0].expr), child(e.left), child(e.right)]
            if _extensions and ir_nodes[args[0]]['width']!=1:args[0]=add(e,'reduce_or',1,[args[0]])
            return add(e, 'mux', w, args, signed=signed)
        if _extensions and k in ('SimpleAssignmentPattern','StructuredAssignmentPattern','ReplicatedAssignmentPattern'):
            if not e.type.isPackedArray:fail(e,'only bound fixed packed assignment patterns supported')
            return add(e,'concat',w,[child(a) for a in e.elements],signed=signed)
        if k == 'Concatenation':
            return add(e, 'concat', w, [child(a) for a in e.operands], signed=signed)
        if k == 'Replication':
            count = integer(e.count, scope)
            if count <= 0:
                fail(e, 'positive constant replication count required')
            capabilities.add('replication')
            return add(e, 'repeat', w, [child(e.concat)], signed=signed, count=count)
        if k in ('ElementSelect', 'RangeSelect'):
            name = reference(e.value)
            typ = e.value.type
            if name in memories:
                if k != 'ElementSelect' or owners_of_signal[name] != owner:
                    fail(e, 'local single memory element read required')
                index = memory_index(e.selector,scope,owner,local_values)
                if _compact:
                    return add(e,'memory_read',w,[index],signed=signed,name=name)
                result = add(e, 'const', w, signed=signed, bits='x'*w)
                for i, word in zip(memories[name]['indices'], memories[name]['words']):
                    value = add(e, 'ref', w, signed=signed, name=word)
                    result = add(e, 'mux', w, [memory_match(e, index, i), value, result], signed=signed)
                return result
            multidimensional = typ.isPackedArray and typ.arrayElementType.isPackedArray
            base = e.value
            while kind(base) in ('ElementSelect', 'RangeSelect'):
                base = base.value
            local_packed = kind(base) == 'NamedValue' and base.symbol in local_symbols
            selector_value = e.selector.constant.value if k == 'ElementSelect' and e.selector.constant is not None else None
            if k == 'ElementSelect' and (not isinstance(selector_value, s.SVInt) or selector_value.hasUnknown):
                if not multidimensional and not local_packed and not _extensions:
                    fail(e, 'dynamic selection supported only for packed word vectors')
                capabilities.add('packed_select')
                return add(e, 'packed_select', w,
                           [child(e.value), child(e.selector)],
                           signed=signed, left=typ.fixedRange.left, right=typ.fixedRange.right,
                           index_signed=bool(e.selector.type.isSigned))
            if k == 'ElementSelect':
                a = b = integer(e.selector, scope)
            else:
                if str(e.selectionKind).split('.')[-1] != 'Simple':
                    fail(e, 'simple static range required')
                a, b = integer(e.left, scope), integer(e.right, scope)
            r = typ.fixedRange
            indices = list(range(a, b + (1 if a <= b else -1), 1 if a <= b else -1))
            if any(i < min(r.left, r.right) or i > max(r.left, r.right) for i in indices):
                if k == 'ElementSelect' and local_packed:
                    return add(e, 'const', w, signed=signed, bits='x'*w)
                fail(e, 'partially out-of-range static range unsupported')
            stride = typ.arrayElementType.bitWidth if typ.isPackedArray else 1
            positions = [abs(i-r.right)*stride+j for i in indices for j in reversed(range(stride))]
            if name in signals and not multidimensional:
                if owners_of_signal[name] != owner or name in clock_signals:
                    fail(e, 'static select of local signal required')
                return add(e, 'select', w, signed=signed, name=name, bits=positions)
            capabilities.add('packed_vectors')
            return add(e, 'slice', w, [child(e.value)], signed=signed, bits=positions)
        if k == 'Call' and e.isSystemCall and e.subroutineName in ('$signed', '$unsigned') and len(e.arguments) == 1:
            arg = child(e.arguments[0])
            return add(e, 'cast_sign', w, [arg], signed=signed)
        fail(e, 'unsupported hardware expression')

    def memory_match(origin, index, value):
        node = ir_nodes[index];w = node['width']
        # Do not truncate a legal array index into the selector's smaller type.
        representable = value < (1 << (w-1 if node['signed'] else w))
        if not representable:
            return add(origin, 'const', 1, bits='0')
        constant = add(origin, 'const', w, bits=format(value, f'0{w}b'), signed=node['signed'])
        return add(origin, 'case_eq', 1, [index, constant], operand_width=w, compare_signed=node['signed'])

    def memory_index(e,scope,owner,local_values):
        value = e.constant.value if e.constant is not None else None
        if isinstance(value,s.SVInt) and not value.hasUnknown:
            constant_audit(e)
            return add(e,'const',value.bitWidth,bits=bits(value),signed=bool(value.isSigned))
        audit_runtime(e.syntax)
        return expression(e,scope,owner,local_values=local_values)

    def memory_target(e, scope):
        """A word or a constant packed slice of a word; no whole-array writes."""
        part = None
        if kind(e) in ('RangeSelect','ElementSelect') and kind(e.value) == 'ElementSelect' and reference(e.value.value) in memories:
            part, e = e, e.value
        if kind(e) != 'ElementSelect' or reference(e.value) not in memories:
            return None
        name = reference(e.value);mem = memories[name];positions = None
        if part is not None:
            if kind(part) == 'ElementSelect':a = b = integer(part.selector, scope)
            else:
                selection = str(part.selectionKind).split('.')[-1]
                if selection == 'Simple':
                    a,b = integer(part.left,scope),integer(part.right,scope)
                elif _compact and selection in ('IndexedUp','IndexedDown'):
                    start,size = integer(part.left,scope),integer(part.right,scope)
                    if size < 1:fail(part,'positive static slice width required')
                    a,b = (start+size-1,start) if selection == 'IndexedUp' else (start,start-size+1)
                    if e.type.fixedRange.left < e.type.fixedRange.right:a,b=b,a
                else:fail(part, 'static memory word slice required')
            typ = e.type;r = typ.fixedRange
            selected = list(range(a,b+(1 if a<=b else -1),1 if a<=b else -1))
            if any(i<min(r.left,r.right) or i>max(r.left,r.right) for i in selected):fail(part, 'memory write slice outside word')
            positions = [abs(i-r.right) for i in selected]
        return name,e.selector,positions

    event_info = None
    bounded_processes = []

    def local_target(e):
        while kind(e) == 'ElementSelect':
            e = e.value
        return e.symbol if kind(e) == 'NamedValue' else None

    def update_local(target, old, value, scope, owner, local_values):
        # Rebuild a packed value from official ranges, rightmost element at
        # offset zero. case_eq guards make unknown / out-of-range writes no-ops.
        if kind(target) == 'NamedValue':
            return value
        if kind(target) != 'ElementSelect':
            fail(target, 'whole local or packed element blocking target required')
        parent = expression(target.value, scope, owner, local_values=local_values)
        typ = target.value.type
        if not typ.isPackedArray:
            fail(target, 'packed array local element required')
        index = memory_index(target.selector, scope, owner, local_values)
        stride = typ.arrayElementType.bitWidth
        r = typ.fixedRange
        chunks = []
        for i in range(r.left, r.right + (1 if r.left <= r.right else -1), 1 if r.left <= r.right else -1):
            offset = abs(i-r.right)*stride
            before = add(target, 'slice', stride, [parent], bits=list(reversed(range(offset, offset+stride))))
            chunks.append(add(target, 'proc_mux', stride, [memory_match(target,index,i), value, before]))
        updated = add(target, 'concat', typ.bitWidth, chunks, signed=bool(typ.isSigned))
        return update_local(target.value, old, updated, scope, owner, local_values)
    if async_reset is not None:
        reset_signal = top + '.' + async_reset
        if (reset_signal not in signals or signals[reset_signal]['direction'] != 'input' or
                signals[reset_signal]['width'] != 1 or len(processes) != 1 or len(hierarchy['instances']) != 1):
            fail(instance, 'async reset requires a one-bit top input and one top-level process without child instances')
    def nba_bounds(statement):
        # v8 reset event sources cannot collapse multiple executed NBA writes
        # into one final vector: intermediate assignments can wake a process.
        # Sequential statements add bounds; exclusive if branches take max.
        k=kind(statement)
        if k=='Block':return nba_bounds(statement.body)
        if k=='List':
            result={}
            for item in statement.list:
                for name,count in nba_bounds(item).items():result[name]=result.get(name,0)+count
            return result
        if k=='Conditional':
            a=nba_bounds(statement.ifTrue);b=nba_bounds(statement.ifFalse) if statement.ifFalse is not None else {}
            return {name:max(a.get(name,0),b.get(name,0)) for name in a.keys()|b.keys()}
        if k=='ExpressionStatement' and kind(statement.expr)=='Assignment' and statement.expr.isNonBlocking:
            name=reference(statement.expr.left)
            return {name:1} if name in signals and name not in memories else {}
        return {}

    for index, (proc, scope, owner) in enumerate(processes):
        body = proc.body
        latch = bounded_events and str(proc.procedureKind).split('.')[-1] == 'AlwaysLatch'
        if kind(body) == 'Timed' and kind(body.timing) == 'SignalEvent':
            audit_reference_owner(body.timing.expr, owner, 'event clock')
        if bounded_events:
            from fourstate_bounded_events import import_process
            info, statement = import_process(proc, reference, kind, fail, owner, audit_reference_owner,**({'_extensions':True} if _extensions else {}))
            info.update(id=index, source=loc(proc))
            bounded_processes.append(info)
        elif async_reset is not None:
            if kind(body) != 'Timed' or kind(body.timing) != 'EventList' or len(body.timing.events) != 2:
                fail(proc, 'exactly two direct posedge events (clock and async reset) required')
            events = body.timing.events
            for event in events:
                if (kind(event) != 'SignalEvent' or str(event.edge).split('.')[-1] != 'PosEdge' or
                        event.iffCondition is not None or kind(event.expr) != 'NamedValue'):
                    fail(proc, 'direct one-bit posedge clock/reset without iff required')
                audit_reference_owner(event.expr, owner, 'async event input')
            if {reference(e.expr) for e in events} != {clock_signal, reset_signal}:
                fail(proc, 'event inputs must be the declared top clock and async reset')
            conditional = body.stmt
            while kind(conditional) == 'Block':
                if conditional.blockSymbol is not None and conditional.blockSymbol.name:
                    fail(conditional, 'named asynchronous process blocks unsupported')
                conditional = conditional.body
            if (kind(conditional) != 'Conditional' or len(conditional.conditions) != 1 or
                    conditional.conditions[0].pattern is not None or
                    reference(conditional.conditions[0].expr) != reset_signal):
                fail(proc, 'async process must begin with ordinary if (reset)')
            from fourstate_events import contract
            event_info = contract(clock, async_reset)
            capabilities.add('async_posedge')
        elif (str(proc.procedureKind).split('.')[-1] not in ('Always', 'AlwaysFF') or kind(body) != 'Timed'
                or kind(body.timing) != 'SignalEvent' or str(body.timing.edge).split('.')[-1] != 'PosEdge'
                or body.timing.iffCondition is not None or reference(body.timing.expr) not in clock_signals):
            fail(proc, 'common direct posedge clock required; asynchronous/derived clocks unsupported')
        def collect_local(n):
            if isinstance(n, s.ast.Statement) and kind(n) == 'VariableDeclaration':
                symbol = n.symbol
                if async_reset is not None:
                    fail(n, 'process locals with async events are outside the F4.3 subset')
                declaration(symbol, process_local=True)
                if str(symbol.lifetime).split('.')[-1] != 'Automatic':
                    fail(n, 'only automatic process locals supported')
                syntax = symbol.syntax.parent
                if str(getattr(syntax, 'constKeyword', '')).strip():
                    fail(n, 'const process locals unsupported')
                if len(processes) != 1 and not bounded_events:
                    fail(n, 'process locals require a single clocked process')
                local_symbols[symbol] = index
                capabilities.add('procedural_locals')
                hierarchy.setdefault('process_locals', []).append({
                    'name': symbol.name, 'path': symbol.hierarchicalPath,
                    'process': index,
                    'width': symbol.type.bitWidth, 'signed': bool(symbol.type.isSigned),
                    'lifetime': 'automatic', 'activation_initial': 'declaration expression' if symbol.initializer is not None else 'x' * symbol.type.bitWidth,
                    'source': loc(n)})
                if symbol.type.isPackedArray:
                    ranges, typ = [], symbol.type
                    while typ.isPackedArray:
                        ranges.append(dict(left=typ.fixedRange.left, right=typ.fixedRange.right,
                                           element_width=typ.arrayElementType.bitWidth))
                        typ = typ.arrayElementType
                    hierarchy['process_locals'][-1]['packed_ranges'] = ranges
        statement = statement if bounded_events else body.stmt
        statement.visit(collect_local)
        def own(n):
            if isinstance(n, s.ast.Expression) and kind(n) == 'Assignment':
                memory = memory_target(n.left,scope)
                if memory is not None:
                    name,selector,positions = memory
                    if not n.isNonBlocking or n.op is not None or n.timingControl is not None:
                        fail(n, 'memory writes require untimed nonblocking assignments')
                    if owners_of_signal[name] != owner:fail(n, 'cross-module memory write unsupported')
                    if _compact and (latch or bounded_processes[index].get('reset')):
                        fail(n,'compact memory requires an edge writer without asynchronous memory reset')
                    for dest in ([name] if _compact else memories[name]['words']):
                        if dest in state_owners and state_owners[dest] != index or dest in drives:
                            fail(n, 'multiple memory writer processes unsupported')
                        state_owners[dest] = index
                    return
                dest = reference(n.left)
                target = local_target(n.left)
                if not n.isNonBlocking:
                    if latch and dest in signals and kind(symbols[dest]) == 'Variable' and n.op is None and n.timingControl is None:
                        if signals[dest]['direction'] == 'input' or owners_of_signal[dest] != owner or dest in drives or dest in state_owners:
                            fail(n, 'single local latch writer required')
                        state_owners[dest] = index
                        return
                    if n.op is not None or n.timingControl is not None or local_symbols.get(target) != index:
                        fail(n, 'whole automatic local blocking assignment required')
                    return
                if (n.op is not None or n.timingControl is not None or target in local_symbols or
                        dest not in signals or kind(symbols[dest]) != 'Variable'):
                    fail(n, 'whole-register nonblocking assignment required')
                if latch:
                    fail(n, 'bounded latch requires blocking assignment')
                if owners_of_signal[dest] != owner or signals[dest]['direction'] == 'input':
                    fail(n, 'local non-input register target required')
                if dest in state_owners and state_owners[dest] != index or dest in drives:
                    fail(n, 'multiple or mixed register drivers: ' + dest)
                state_owners[dest] = index
        statement.visit(own)
        if _eventful:scalar_nba_bounds.update(nba_bounds(statement))
        process_bodies.append((statement, scope, owner, proc))

    assertions, block_stack = [], []
    current_process = None

    def lower(statement, env, local_values, scope, owner, guard=None):
        k = kind(statement)
        if k == 'Empty':
            return env, local_values
        if k == 'Block':
            if str(statement.blockKind).split('.')[-1] != 'Sequential':
                fail(statement, 'sequential begin/end required')
            if statement.blockSymbol is not None and statement.blockSymbol.name:
                hierarchy.setdefault('named_process_blocks', []).append(dict(name=statement.blockSymbol.name, source=loc(statement)))
            named = statement.blockSymbol is not None and bool(statement.blockSymbol.name)
            if named:block_stack.append(statement.blockSymbol.hierarchicalPath)
            try:result, after = lower(statement.body, env, dict(local_values), scope, owner, guard)
            finally:
                if named:block_stack.pop()
            return result, {symbol: after[symbol] for symbol in local_values}
        if k == 'List':
            for item in statement.list:
                env, local_values = lower(item, env, local_values, scope, owner, guard)
            return env, local_values
        if k == 'VariableDeclaration':
            symbol = statement.symbol
            if symbol not in local_symbols:
                fail(statement, 'unregistered process local')
            result = dict(local_values)
            result[symbol] = add(statement, 'const', symbol.type.bitWidth,
                                 signed=bool(symbol.type.isSigned), bits='x' * symbol.type.bitWidth)
            if symbol.initializer is not None:
                audit_runtime(symbol.initializer.syntax)
                result[symbol] = expression(symbol.initializer, scope, owner, local_values=result)
            return env, result
        if k == 'ExpressionStatement' and kind(statement.expr) == 'Assignment':
            e = statement.expr
            audit_runtime(e.right.syntax)
            value = expression(e.right, scope, owner, local_values=local_values)
            if e.isNonBlocking:
                result = dict(env)
                memory = memory_target(e.left,scope)
                if memory is not None:
                    name,selector,positions = memory
                    index = memory_index(selector,scope,owner,local_values)
                    if _compact:
                        result[name] = add(e,'memory_write',signals[name]['width'],[env[name],index,value],
                            name=name,positions=positions,process=current_process)
                        return result,local_values
                    for i,word in zip(memories[name]['indices'],memories[name]['words']):
                        candidate = value
                        if positions is not None:
                            # Earlier NBAs to disjoint/overlapping bits survive
                            # except where this later statement overwrites them.
                            args = [add(e,'slice',1,[value],bits=[len(positions)-1-positions.index(bit)]) if bit in positions else
                                    add(e,'slice',1,[env[word]],bits=[bit])
                                    for bit in reversed(range(signals[word]['width']))]
                            candidate = add(e,'concat',signals[word]['width'],args)
                        result[word] = add(e,'proc_mux',signals[word]['width'],[memory_match(e,index,i),candidate,env[word]],signed=signals[word]['signed'])
                    return result,local_values
                result[reference(e.left)] = value
                return result, local_values
            target = local_target(e.left)
            if bounded_events and reference(e.left) in env:
                result = dict(env); result[reference(e.left)] = value
                return result, local_values
            if target not in local_values:
                fail(e, 'blocking assignment outside active local scope')
            result = dict(local_values)
            result[target] = update_local(e.left, local_values[target], value, scope, owner, local_values)
            return env, result
        if k == 'Conditional':
            if len(statement.conditions) != 1 or statement.conditions[0].pattern is not None or 'None' not in str(statement.check):
                fail(statement, 'ordinary one-bit procedural if required')
            cond = statement.conditions[0].expr
            if cond.type.bitWidth != 1 and not _extensions:
                fail(cond, 'one-bit procedural if required')
            audit_runtime(cond.syntax)
            condition = expression(cond, scope, owner, local_values=local_values)
            if _extensions and ir_nodes[condition]['width']!=1:condition=add(cond,'reduce_or',1,[condition])
            yes_guard = no_guard = guard
            if immediate_assertions:
                one = add(cond,'const',1,bits='1')
                taken = add(cond,'case_eq',1,[condition,one],operand_width=1,compare_signed=False)
                yes_guard = add(cond,'and',1,[guard,taken])
                no_guard = add(cond,'and',1,[guard,add(cond,'not',1,[taken])])
            yes, yes_locals = lower(statement.ifTrue, dict(env), dict(local_values), scope, owner, yes_guard)
            no, no_locals = (lower(statement.ifFalse, dict(env), dict(local_values), scope, owner, no_guard)
                            if statement.ifFalse is not None else (env, local_values))
            result = {name: add(statement, 'memory_merge' if _compact and name in memories else 'proc_mux', signals[name]['width'], [condition, yes[name], no[name]],
                                signed=signals[name]['signed']) if yes[name] != no[name] else yes[name] for name in env}
            locals_result = {symbol: add(statement, 'proc_mux', symbol.type.bitWidth,
                                [condition, yes_locals[symbol], no_locals[symbol]], signed=bool(symbol.type.isSigned))
                             if yes_locals[symbol] != no_locals[symbol] else yes_locals[symbol]
                             for symbol in local_values}
            return result, locals_result
        if _extensions and (k=='ImmediateAssertion' or k=='ExpressionStatement' and kind(statement.expr)=='Call'):
            from fourstate_diagnostics import action
            a=action(statement,kind,fail,integer,scope)
            if bounded_processes[current_process]['kind']!='edge':fail(statement,'diagnostics require an edge process')
            passed=add(statement,'const',1,bits='0')
            if a['condition'] is not None:
                audit_runtime(a['condition'].syntax);value=expression(a['condition'],scope,owner,local_values=local_values)
                if ir_nodes[value]['width']!=1:value=add(statement,'reduce_or',1,[value])
                passed=add(statement,'case_eq',1,[value,add(statement,'const',1,bits='1')],operand_width=1,compare_signed=False)
            args=[]
            for e in a['payload']:
                audit_runtime(e.syntax);value=expression(e,scope,owner,local_values=local_values)
                args.append(dict(node=value,width=ir_nodes[value]['width'],signed=ir_nodes[value]['signed']))
            pos=loc(statement);path=block_stack[-1] if block_stack else owner
            assertions.append(dict(id=f'{path}@{Path(pos["file"]).name}:{pos["line"]}:{pos["column"]}',process=current_process,
                ordinal=sum(x['process']==current_process for x in assertions),label=None,rtl_scope=path[len(top)+1:] if path!=top else '',
                source=pos,action_source=loc(a['call']),action_end_source=location(sm,a['call'].sourceRange.end),severity=a['severity'],message=a['format'],action=a['action'],stream=a['stream'],
                specifiers=a['specifiers'],arguments=args,enable_node=guard,pass_node=passed))
            capabilities.add('sampled_diagnostics')
            return env,local_values
        if k == 'ImmediateAssertion' and immediate_assertions:
            if (str(statement.assertionKind).split('.')[-1] != 'Assert' or statement.isDeferred or statement.isFinal or
                    statement.ifTrue is not None and kind(statement.ifTrue) != 'Empty'):
                fail(statement, 'only immediate assert with empty success action and static $error failure supported')
            action = statement.ifFalse
            if action is None or kind(action) != 'ExpressionStatement' or kind(action.expr) != 'Call':
                fail(statement, 'immediate assertion requires direct static $error failure action')
            call = action.expr
            if (not call.isSystemCall or call.subroutineName != '$error' or len(call.arguments) != 1 or
                    kind(call.arguments[0]) != 'StringLiteral' or '%' in call.arguments[0].value):
                fail(statement, 'only one literal nonformatted $error string is supported')
            message = call.arguments[0].value
            if '\n' in message.rstrip('\n') or any(ord(c)<32 and c!='\n' for c in message):
                fail(statement, 'single-line $error message with optional trailing newline required')
            if bounded_processes[current_process]['kind'] != 'edge' or statement.cond.type.bitWidth != 1:
                fail(statement, 'one-bit immediate assertion in bounded edge process required')
            audit_runtime(statement.cond.syntax)
            value = expression(statement.cond, scope, owner, local_values=local_values)
            passed = add(statement,'case_eq',1,[value,add(statement,'const',1,bits='1')],operand_width=1,compare_signed=False)
            label = statement.syntax.label
            label = label.name.valueText if label is not None else None
            pos = loc(statement);path = block_stack[-1] if block_stack else owner
            assertions.append(dict(id=f'{path}@{Path(pos["file"]).name}:{pos["line"]}:{pos["column"]}',
                process=current_process,ordinal=sum(a['process']==current_process for a in assertions),label=label,
                rtl_scope=path[len(top)+1:] if path != top else '',source=pos,condition_source=loc(statement.cond),
                action_source=loc(call),severity='error',message=call.arguments[0].value,
                enable_node=guard,pass_node=passed))
            capabilities.add('immediate_assertions')
            return env, local_values
        fail(statement, 'unsupported clocked statement')

    assignments = {}
    for dest, (right, scope, owner, placeholder, origin) in drives.items():
        if dest in clock_signals:
            continue
        if placeholder is None:
            audit_runtime(right.syntax)
        assignments[dest] = expression(right, scope, owner, placeholder)
        if ir_nodes[assignments[dest]]['width'] != signals[dest]['width']:
            fail(origin, 'bound assignment width mismatch')
    next_state = {}
    scalable_groups = {}
    if _scalable:
        for name, process in state_owners.items():
            scalable_groups.setdefault(process, []).append(name)
    for i, (body, scope, owner, proc) in enumerate(process_bodies):
        current_process = i
        env = {n: add(proc, 'memory_ref' if _compact and n in memories else 'ref', signals[n]['width'], signed=signals[n]['signed'], name=n)
               for n in (scalable_groups.get(i, []) if _scalable else (n for n, p in state_owners.items() if p == i))}
        lowered, _ = lower(body, env, {}, scope, owner, add(proc,'const',1,bits='1') if immediate_assertions else None)
        next_state.update(lowered)
    if mode == 'sequential' and not state_owners and not _extensions:
        fail(instance, 'at least one assigned register required')
    for name, info in signals.items():
        if name not in clock_signals and info['direction'] != 'input' and name not in assignments and name not in state_owners:
            if not _extensions:fail(symbols[name], 'undriven signal: ' + name)
            value=info.get('floating_bits',('z' if kind(symbols[name])=='Net' else 'x')*info['width'])
            assignments[name]=add(symbols[name],'const',info['width'],bits=value,signed=info['signed'])
            hierarchy.setdefault('floating_signals',[]).append(dict(name=name,bits=value,source=info['source']))
    order, active, done = [], set(), set()
    def dependencies(i):
        n = ir_nodes[i]
        if n['op'] in ('ref', 'select', 'memory_read', 'memory_ref'):
            yield n['name']
        for a in n['args']:
            yield from dependencies(a)
    def schedule(name):
        if name in active:
            fail(symbols[name], 'combinational cycle: ' + name)
        if name in done or name in state_owners or signals[name]['direction'] == 'input':
            return
        active.add(name)
        for dep in dependencies(assignments[name]):
            schedule(dep)
        active.remove(name); done.add(name); order.append(name)
    if _scalable:
        from fourstate_scalable_events import combinational_order
        order = combinational_order(signals, ir_nodes, assignments, state_owners, fail, symbols)
    else:
        for name in assignments:
            schedule(name)
    states = [dict(signals[n], process=p,
                   **({} if _compact else dict(bit_indices=[signals[n]['right'] + (i if signals[n]['left'] >= signals[n]['right'] else -i)
                                for i in range(signals[n]['width'])]))) for n, p in state_owners.items()]
    hierarchy['states'] = [{k: x[k] for k in ('name', 'rtl_path', 'width', 'signed', 'left', 'right', 'source')} for x in states]
    monitors = []
    if bounded_events:
        if _scalable:
            from fourstate_scalable_events import finish_contract
        else:
            from fourstate_bounded_events import finish_contract
        event_info, monitors = finish_contract(top, roots, bounded_processes, signals, ir_nodes, assignments,
                                               next_state, states, dependencies, fail, symbols, assertions=assertions,
                                               reset_aliases=reset_aliases, _memory_names=tuple(memories) if _compact else (),
                                               **({'_eventful': True, '_scalar_nba_bounds': scalar_nba_bounds} if _eventful else {}),
                                               **({'_extensions': True} if _extensions else {}))
        capabilities.add('bounded_events')
        hierarchy['events'] = event_info
    if memories:hierarchy['memories'] = list(memories.values())
    extended = capabilities - {'net_initializer'}
    preprocessed = bool(defines or include_dirs or pp['directives'])
    if preprocessed:
        capabilities.add('preprocessing')
        extended.add('preprocessing')
    return {'format': 'chisel2lean-fourstate-ir-v9' if _extensions else 'chisel2lean-fourstate-ir-v8' if _eventful else 'chisel2lean-fourstate-ir-v7' if _scalable else 'chisel2lean-fourstate-ir-v6' if _compact else 'chisel2lean-fourstate-ir-v5' if extended else 'chisel2lean-fourstate-ir-v4',
            'capabilities': sorted(capabilities), 'mode': mode, 'top': top,
            'source': str(Path(source).resolve()), 'sources': [str(p) for p in files],
            'signals': list(signals.values()), 'nodes': ir_nodes, 'assignments': assignments, 'order': order,
            'parser': 'pyslang 12.0.0', 'clock': clock if mode == 'sequential' else None,
            'clock_signal': clock_signal if mode == 'sequential' else None, 'clock_signals': sorted(clock_signals),
            'states': states, 'next_state': next_state, 'elaboration': hierarchy,
            **({'scalar_nba_bounds': scalar_nba_bounds} if _eventful else {}),
            **({'memories': list(memories.values())} if memories else {}),
            **({'assertions': assertions} if assertions else {}),
            **({'preprocessing': pp} if preprocessed else {}),
            **({'events': event_info} if event_info else {})}
