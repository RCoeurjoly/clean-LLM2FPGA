"""Strict, source-audited pyslang frontend for the four-state combinational subset."""
from __future__ import annotations

from pathlib import Path
import re

from bootstrap_fourstate import load_parser


OPS = {'UnaryBitwiseNotExpression': 'not', 'BinaryAndExpression': 'and',
       'BinaryOrExpression': 'or', 'BinaryXorExpression': 'xor',
       'BinaryXnorExpression': 'xnor'}
ARITHMETIC = {'AddExpression': 'add', 'SubtractExpression': 'sub'}
SHIFTS = {'LogicalShiftLeftExpression': 'shl', 'LogicalShiftRightExpression': 'shr',
          'ArithmeticShiftLeftExpression': 'ashl', 'ArithmeticShiftRightExpression': 'ashr'}
COMPARISONS = {'EqualityExpression': 'eq', 'InequalityExpression': 'ne',
               'CaseEqualityExpression': 'case_eq', 'CaseInequalityExpression': 'case_ne',
               'LessThanExpression': 'lt', 'LessThanEqualExpression': 'le',
               'GreaterThanExpression': 'gt', 'GreaterThanEqualExpression': 'ge'}
LOGICAL = {'UnaryLogicalNotExpression': 'logical_not', 'LogicalAndExpression': 'logical_and',
           'LogicalOrExpression': 'logical_or'}
ALLOWED = set(OPS) | {
    'CompilationUnit', 'ModuleDeclaration', 'ModuleHeader', 'AnsiPortList',
    'ImplicitAnsiPort', 'NetPortHeader', 'VariablePortHeader', 'ImplicitType',
    'LogicType', 'VariableDimension', 'RangeDimensionSpecifier', 'SimpleRangeSelect',
    'IntegerLiteralExpression', 'Declarator', 'NetDeclaration', 'DataDeclaration',
    'ContinuousAssign', 'AssignmentExpression', 'IdentifierName',
    'ConditionalExpression', 'ConditionalPredicate', 'ConditionalPattern',
    'ParenthesizedExpression', 'ConcatenationExpression', 'IdentifierSelectName',
    'ElementSelect', 'BitSelect', 'IntegerVectorExpression', 'CastExpression',
    'InvocationExpression', 'SystemName', 'ArgumentList', 'OrderedArgument',
    'SimplePropertyExpr', 'SimpleSequenceExpr'} | set(ARITHMETIC) | set(SHIFTS) | set(COMPARISONS) | set(LOGICAL) | {
    'UnaryMinusExpression', 'UnaryPlusExpression'}


def parse(source: Path, top: str, mode='combinational', clock=None, *, defines=(), include_dirs=(), preprocessing=None, async_reset=None, bounded_memories=False, bounded_events=False, event_clocks=(), immediate_assertions=False):
    if mode not in ('combinational', 'sequential') or (mode == 'sequential') != bool(clock):
        raise ValueError('Sequential mode requires --clock; combinational mode forbids it')
    sequential = mode == 'sequential'
    if source.is_dir() or defines or include_dirs or preprocessing is not None or async_reset is not None or bounded_memories or bounded_events or event_clocks or immediate_assertions:
        from fourstate_elaborated import parse as parse_elaborated
        return parse_elaborated(source, top, mode, clock, defines=defines, include_dirs=include_dirs, preprocessing=preprocessing, async_reset=async_reset, bounded_memories=bounded_memories, bounded_events=bounded_events, event_clocks=event_clocks, immediate_assertions=immediate_assertions)
    allowed = ALLOWED | ({'RegType', 'AlwaysBlock', 'AlwaysFFBlock', 'TimingControlStatement',
        'EventControlWithExpression', 'ParenthesizedEventExpression', 'SignalEventExpression',
        'SequentialBlockStatement', 'ConditionalStatement', 'ElseClause', 'ExpressionStatement',
        'NonblockingAssignmentExpression', 'EmptyStatement'} if sequential else set())
    s = load_parser()
    source = source.resolve()
    raw = source.read_text(encoding='utf-8')
    # Macro-bearing files go through official preprocessing and symbol binding.
    uncommented = re.sub(r'/\*.*?\*/|//[^\n]*', lambda m: ''.join('\n' if c == '\n' else ' ' for c in m[0]), raw, flags=re.S)
    if '`' in uncommented:
        from fourstate_elaborated import parse as parse_elaborated
        return parse_elaborated(source, top, mode, clock)
    tree = s.syntax.SyntaxTree.fromFile(str(source))
    sm = tree.sourceManager
    structural, modules_seen = [], []
    def _syntax_parents(n):
        parent = n.parent
        while parent is not None:
            yield parent
            parent = parent.parent
    def inspect(n):
        if isinstance(n, s.syntax.SyntaxNode):
            k = str(n.kind).split('.')[-1]
            if k == 'ModuleDeclaration':
                modules_seen.append(n)
            if sequential and any(getattr(n, attr, None) is not None for attr in ('label', 'blockName', 'endBlockName')):
                # Labels and nested scopes belong to slang's bound block AST.
                structural.append(n)
            if sequential and (k == 'DataDeclaration' or
                    (k == 'AssignmentExpression' and str(n.parent.kind).split('.')[-1] != 'ContinuousAssign')):
                # Let official symbol binding handle local scopes and blocking
                # order; do not duplicate that environment in the flat parser.
                if k == 'AssignmentExpression' or any(
                        str(p.kind).split('.')[-1] in ('AlwaysBlock', 'AlwaysFFBlock')
                        for p in _syntax_parents(n)):
                    structural.append(n)
            if ('Generate' in k or k in ('HierarchyInstantiation', 'ParameterDeclaration', 'ParameterPortList', 'LocalParameterDeclaration')):
                structural.append(n)
            if k in ('MultipleConcatenationExpression', 'UnaryBitwiseAndExpression', 'UnaryBitwiseOrExpression'):
                structural.append(n)
            if k == 'NetDeclaration' and any(isinstance(d, s.syntax.SyntaxNode) and d.initializer is not None for d in n.declarators):
                structural.append(n)
            if k in ('ImplicitType', 'LogicType') and len(n.dimensions) > 1:
                structural.append(n)
    tree.root.visit(inspect)
    if structural or len(modules_seen) != 1:
        from fourstate_elaborated import parse as parse_elaborated
        return parse_elaborated(source, top, mode, clock)

    def loc(n):
        p = n.sourceRange.start
        return {'file': str(source), 'line': sm.getLineNumber(p), 'column': sm.getColumnNumber(p)}

    def fail(n, message):
        p = loc(n)
        raise ValueError(f"{p['file']}:{p['line']}:{p['column']}: {message} ({kind(n)})")

    def kind(n):
        return str(n.kind).split('.')[-1]

    def nodes(items):
        return [n for n in items if isinstance(n, s.syntax.SyntaxNode)]

    def audit(n):
        if not isinstance(n, s.syntax.SyntaxNode):
            return
        if kind(n) not in allowed:
            fail(n, 'unsupported syntax')
        if getattr(n, 'attributes', None):
            fail(n, 'attributes unsupported')
        for attr in ('delay', 'strength', 'initializer', 'parameters', 'matchesClause'):
            if not (kind(n) == 'ArgumentList' and attr == 'parameters') and getattr(n, attr, None) is not None:
                fail(n, attr + ' unsupported')
        for attr in ('iffClause', 'label', 'blockName', 'endBlockName'):
            if sequential and getattr(n, attr, None) is not None:
                fail(n, attr + ' unsupported')
        if sequential and str(getattr(n, 'uniqueOrPriority', '')).strip():
            fail(n, 'unique/priority unsupported')
        for attr in ('signing', 'netType', 'varKeyword', 'constKeyword', 'lifetime', 'expansionHint'):
            token = getattr(n, attr, None)
            if token is not None and str(token).strip() not in ('', 'unsigned', 'signed', 'wire'):
                fail(n, attr + ' unsupported')
        if kind(n) == 'Declarator' and len(n.dimensions):
            fail(n, 'unpacked dimensions unsupported')
        if kind(n) in ('ImplicitType', 'LogicType', 'RegType') and len(n.dimensions) > 1:
            fail(n, 'multiple packed dimensions unsupported')
        if kind(n) == 'AssignmentExpression' and kind(n.parent) != 'ContinuousAssign':
            fail(n, 'assignment expression outside continuous assignment')

    tree.root.visit(audit)
    if any(d.isError() for d in tree.diagnostics):
        fail(tree.root, s.DiagnosticEngine.reportAll(sm, tree.diagnostics))
    modules = [tree.root] if kind(tree.root) == 'ModuleDeclaration' else nodes(tree.root.members)
    if len(modules) != 1 or kind(modules[0]) != 'ModuleDeclaration':
        fail(tree.root, 'exactly one module required')
    module = modules[0]
    if str(module.header.name).strip() != top:
        fail(module, 'top does not match the sole module')
    if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_$]*', top):
        fail(module, 'simple module identifier required')
    if module.header.ports is None or kind(module.header.ports) != 'AnsiPortList':
        fail(module, 'ANSI port list required')

    def integer(n):
        if kind(n) != 'IntegerLiteralExpression' or not re.fullmatch(r'[0-9][0-9_]*', str(n).strip()):
            fail(n, 'nonnegative decimal literal index required')
        return int(str(n).strip().replace('_', ''))

    # Validate ranges before semantic elaboration can fold parameter expressions.
    def ranges(n):
        if isinstance(n, s.syntax.SyntaxNode) and kind(n) == 'SimpleRangeSelect':
            integer(n.left); integer(n.right)
    tree.root.visit(ranges)
    compilation = s.ast.Compilation()
    compilation.addSyntaxTree(tree)
    root = compilation.getRoot()
    diagnostics = compilation.getAllDiagnostics()
    if any(d.isError() for d in diagnostics):
        fail(module, s.DiagnosticEngine.reportAll(sm, diagnostics))
    body = root.topInstances[0].body
    directions = {p.name: {'In': 'input', 'Out': 'output'}.get(str(p.direction).split('.')[-1], 'unsupported') for p in body.portList}
    signals = {}
    variable_names = set()
    for symbol in body:
        if str(symbol.kind).split('.')[-1] not in ('Net', 'Variable'):
            continue
        name = symbol.name
        if str(symbol.kind).split('.')[-1] == 'Variable':
            variable_names.add(name)
        if not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_$]*', name):
            fail(symbol.syntax, 'simple signal identifier required')
        typ = symbol.type
        if not typ.isSimpleBitVector or not typ.isFourState or typ.bitWidth < 1:
            fail(symbol.syntax, 'fixed four-state vector required')
        direction = directions.get(name, 'internal')
        if direction not in ('input', 'output', 'internal'):
            fail(symbol.syntax, 'inout/ref ports unsupported')
        r = typ.fixedRange
        signals[name] = {'name': name, 'direction': direction, 'width': typ.bitWidth,
                         'signed': bool(typ.isSigned),
                         'left': r.left, 'right': r.right, 'source': loc(symbol.syntax)}
    for n in signals:
        for suffix in ('__tag', '__value'):
            if n + suffix in signals:
                fail(module, 'encoded name collision: ' + n + suffix)
    if not any(p['direction'] == 'output' for p in signals.values()):
        fail(module, 'at least one output required')
    processes = [m for m in module.members if kind(m) in ('AlwaysBlock', 'AlwaysFFBlock')]
    owners = {}
    if sequential:
        if clock not in signals or signals[clock]['width'] != 1 or signals[clock]['direction'] != 'input':
            fail(module, 'clock must name a one-bit input')
        if not processes:
            fail(module, 'sequential mode requires a clocked process')
        for index, process in enumerate(processes):
            timed = process.statement
            if kind(timed) != 'TimingControlStatement' or kind(timed.timingControl) != 'EventControlWithExpression':
                fail(process, 'direct posedge event required')
            event = timed.timingControl.expr
            if kind(event) == 'ParenthesizedEventExpression':
                event = event.expr
            if (kind(event) != 'SignalEventExpression' or str(event.edge).strip() != 'posedge'
                    or kind(event.expr) != 'IdentifierName' or str(event.expr.identifier).strip() != clock):
                fail(event, 'all processes must use the same direct posedge clock')

            def collect(n):
                if not isinstance(n, s.syntax.SyntaxNode):
                    return
                if kind(n) == 'DataDeclaration':
                    fail(n, 'process-local declarations unsupported')
                if kind(n) != 'NonblockingAssignmentExpression':
                    return
                if kind(n.left) != 'IdentifierName':
                    fail(n, 'whole-register nonblocking assignment required')
                name = str(n.left.identifier).strip()
                if name not in variable_names or signals[name]['direction'] == 'input':
                    fail(n, 'register target must be a declared non-input variable')
                if name in owners and owners[name] != index:
                    fail(n, 'multiple process drivers for ' + name)
                owners[name] = index
            timed.statement.visit(collect)
        if not owners:
            fail(module, 'at least one assigned register required')
    assignments = {}
    ir_nodes = []

    def add(n, op, width, args=(), signed=False, **extra):
        i = len(ir_nodes)
        ir_nodes.append({'id': i, 'op': op, 'width': width, 'signed': signed,
                         'args': list(args), 'source': loc(n), **extra})
        return i

    def width(i):
        return ir_nodes[i]['width']

    def literal(n):
        spelling = re.sub(r'\s|_', '', str(n)).lower()
        m = re.fullmatch(r"([0-9]+)'(s?)([bohd])([0-9a-fxz]+)", spelling)
        if not m or int(m[1]) < 1:
            fail(n, 'explicit positive width literal required')
        w, signed, base, digits = int(m[1]), bool(m[2]), m[3], m[4]
        if base == 'd':
            if digits in ('x', 'z'):
                bits = digits * w
            elif digits.isdecimal():
                bits = format(int(digits), 'b').zfill(w)[-w:]
            else:
                fail(n, 'invalid decimal constant')
        else:
            step, radix = {'b': (1, 2), 'o': (3, 8), 'h': (4, 16)}[base]
            bits = ''
            for digit in digits:
                if digit in 'xz':
                    bits += digit * step
                elif int(digit, 16) < radix:
                    bits += format(int(digit, 16), f'0{step}b')
                else:
                    fail(n, 'invalid constant digit')
            fill = bits[0] if bits[0] in 'xz' else '0'
            bits = (fill * w + bits)[-w:]
        return bits, signed

    def invocation_argument(n):
        if str(n.left).strip() not in ('$signed', '$unsigned'):
            fail(n, 'only $signed/$unsigned conversions supported')
        args = nodes(n.arguments.parameters)
        if len(args) != 1 or kind(args[0]) != 'OrderedArgument':
            fail(n, 'conversion requires one positional argument')
        arg = args[0].expr
        while kind(arg) in ('SimplePropertyExpr', 'SimpleSequenceExpr'):
            arg = arg.expr
        return arg

    def intrinsic(n):
        k = kind(n)
        if k in ('ParenthesizedExpression', 'SimplePropertyExpr', 'SimpleSequenceExpr'):
            return intrinsic(n.expression if k == 'ParenthesizedExpression' else n.expr)
        if k == 'IdentifierName':
            name = str(n.identifier).strip()
            if name not in signals:
                fail(n, 'undeclared signal ' + name)
            if sequential and name == clock:
                fail(n, 'clock cannot be used as data')
            return signals[name]['width'], signals[name]['signed']
        if k == 'IdentifierSelectName':
            name = str(n.identifier).strip()
            if name not in signals:
                fail(n, 'undeclared signal ' + name)
            if sequential and name == clock:
                fail(n, 'clock cannot be used as data')
            selectors = nodes(n.selectors)
            if len(selectors) != 1:
                fail(n, 'one static selector required')
            sel = selectors[0].selector
            if kind(sel) == 'BitSelect':
                a = b = integer(sel.expr)
            elif kind(sel) == 'SimpleRangeSelect':
                a, b = integer(sel.left), integer(sel.right)
            else:
                fail(n, 'static bit or range select required')
            left, right = signals[name]['left'], signals[name]['right']
            indices = list(range(a, b + (1 if a <= b else -1), 1 if a <= b else -1))
            if any(not min(left, right) <= i <= max(left, right) for i in indices):
                fail(n, 'select outside declaration')
            if a != b and (a > b) != (left > right):
                fail(n, 'select direction does not match declaration')
            return len(indices), False
        if k == 'IntegerVectorExpression':
            bits, signed = literal(n)
            return len(bits), signed
        if k == 'IntegerLiteralExpression':
            fail(n, 'unsized literals unsupported; use explicit width')
        if k == 'InvocationExpression':
            arg = invocation_argument(n)
            return intrinsic(arg)[0], str(n.left).strip() == '$signed'
        if k == 'CastExpression':
            if kind(n.left) != 'IntegerLiteralExpression':
                fail(n, 'fixed decimal width cast required')
            w = integer(n.left)
            if w < 1:
                fail(n, 'positive width cast required')
            return w, intrinsic(n.right)[1]
        if k == 'ConcatenationExpression':
            args = nodes(n.expressions)
            if not args:
                fail(n, 'empty concatenation unsupported')
            return sum(intrinsic(x)[0] for x in args), False
        if k in LOGICAL or k in COMPARISONS:
            return 1, False
        if k == 'ConditionalExpression':
            a, b = intrinsic(n.left), intrinsic(n.right)
            return max(a[0], b[0]), a[1] and b[1]
        if k in OPS or k in ARITHMETIC:
            if k == 'UnaryBitwiseNotExpression':
                return intrinsic(n.operand)
            a, b = intrinsic(n.left), intrinsic(n.right)
            return max(a[0], b[0]), a[1] and b[1]
        if k in SHIFTS:
            return intrinsic(n.left)
        if k in ('UnaryMinusExpression', 'UnaryPlusExpression'):
            return intrinsic(n.operand)
        fail(n, 'unsupported expression')

    def fit(n, i, target, signed=None):
        old = ir_nodes[i]
        effective_signed = old['signed'] if signed is None else signed
        if old['width'] == target and old['signed'] == effective_signed:
            return i
        if old['width'] == target:
            return add(n, 'cast_sign', target, [i], signed=effective_signed)
        return add(n, 'resize', target, [i], signed=effective_signed,
                   from_width=old['width'], extension='sign' if effective_signed else 'zero')

    def expr(n, context_width=None, context_signed=None):
        k = kind(n)
        if k in ('ParenthesizedExpression', 'SimplePropertyExpr', 'SimpleSequenceExpr'):
            return expr(n.expression if k == 'ParenthesizedExpression' else n.expr,
                        context_width, context_signed)
        natural_width, natural_signed = intrinsic(n)
        effective_signed = natural_signed if context_signed is None else context_signed
        target = max(natural_width, context_width or 0)
        if k in ('IdentifierName', 'IdentifierSelectName'):
            name = str(n.identifier).strip()
            info = signals[name]
            if k == 'IdentifierName':
                return fit(n, add(n, 'ref', info['width'], signed=info['signed'], name=name),
                           target, effective_signed)
            selectors = nodes(n.selectors)
            sel = selectors[0].selector
            if kind(sel) == 'BitSelect':
                a = b = integer(sel.expr)
            else:
                a, b = integer(sel.left), integer(sel.right)
            indices = list(range(a, b + (1 if a <= b else -1), 1 if a <= b else -1))
            bits = [abs(i - info['right']) for i in indices]
            return fit(n, add(n, 'select', len(bits), name=name, bits=bits), target, effective_signed)
        if k == 'IntegerVectorExpression':
            bits, signed = literal(n)
            return fit(n, add(n, 'const', len(bits), signed=signed, bits=bits), target, effective_signed)
        if k == 'InvocationExpression':
            arg = expr(invocation_argument(n))
            return fit(n, arg, target, effective_signed)
        if k == 'CastExpression':
            # A size cast supplies its own width to context-determined operands.
            # Keep the outer assignment width outside this cast boundary.
            arg = expr(n.right, natural_width)
            cast = fit(n, arg, natural_width)
            return fit(n, cast, target, effective_signed)
        if k in OPS or k in ARITHMETIC:
            op = OPS.get(k, ARITHMETIC.get(k))
            args = ([expr(n.operand, target, effective_signed)] if k == 'UnaryBitwiseNotExpression' else
                    [expr(n.left, target, effective_signed), expr(n.right, target, effective_signed)])
            return add(n, op, target, args, signed=effective_signed)
        if k in ('UnaryMinusExpression', 'UnaryPlusExpression'):
            arg = expr(n.operand, target, effective_signed)
            return add(n, 'neg' if k == 'UnaryMinusExpression' else 'pos', target, [arg], signed=effective_signed)
        if k in SHIFTS:
            left = expr(n.left, target, effective_signed)
            right = expr(n.right)
            return add(n, SHIFTS[k], target, [left, right], signed=effective_signed)
        if k == 'ConcatenationExpression':
            args = [expr(x) for x in nodes(n.expressions)]
            value = add(n, 'concat', sum(map(width, args)), args)
            return fit(n, value, target, effective_signed)
        if k == 'ConditionalExpression':
            conditions = nodes(n.predicate.conditions)
            if len(conditions) != 1:
                fail(n, 'one conditional predicate required')
            condition = expr(conditions[0].expr)
            if width(condition) != 1:
                fail(n, 'one-bit condition required')
            args = [condition, expr(n.left, target, effective_signed), expr(n.right, target, effective_signed)]
            return add(n, 'mux', target, args, signed=effective_signed)
        if k in LOGICAL:
            args = [expr(n.operand)] if k == 'UnaryLogicalNotExpression' else [expr(n.left), expr(n.right)]
            value = add(n, LOGICAL[k], 1, args)
            return fit(n, value, target, effective_signed)
        if k in COMPARISONS:
            left_type, right_type = intrinsic(n.left), intrinsic(n.right)
            common_width = max(left_type[0], right_type[0])
            common_signed = left_type[1] and right_type[1]
            args = [expr(n.left, common_width, common_signed), expr(n.right, common_width, common_signed)]
            value = add(n, COMPARISONS[k], 1, args, compare_signed=common_signed,
                        operand_width=common_width)
            return fit(n, value, target, effective_signed)
        fail(n, 'unsupported expression')

    for member in module.members:
        if kind(member) != 'ContinuousAssign':
            continue
        for assignment in nodes(member.assignments):
            if kind(assignment.left) != 'IdentifierName':
                fail(assignment.left, 'whole-signal assignment required')
            name = str(assignment.left.identifier).strip()
            if name not in signals or signals[name]['direction'] == 'input':
                fail(assignment, 'assignment target must be declared output or internal signal')
            if name in assignments:
                fail(assignment, 'multiple drivers for ' + name)
            if name in owners:
                fail(assignment, 'mixed continuous and process drivers for ' + name)
            value = fit(assignment, expr(assignment.right, signals[name]['width']), signals[name]['width'])
            assignments[name] = value
    next_state = {}
    for index, process in enumerate(processes):
        registers = [name for name in owners if owners[name] == index]
        initial = {name: add(process, 'ref', signals[name]['width'], signed=signals[name]['signed'], name=name) for name in registers}

        def lower(statement, incoming):
            env = dict(incoming)
            k = kind(statement)
            if k == 'EmptyStatement':
                return env
            if k == 'SequentialBlockStatement':
                for item in statement.items:
                    env = lower(item, env)
                return env
            if k == 'ExpressionStatement' and kind(statement.expr) == 'NonblockingAssignmentExpression':
                n = statement.expr
                name = str(n.left.identifier).strip()
                value = fit(n, expr(n.right, signals[name]['width']), signals[name]['width'])
                env[name] = value
                return env
            if k == 'ConditionalStatement':
                conditions = nodes(statement.predicate.conditions)
                if len(conditions) != 1:
                    fail(statement, 'one procedural condition required')
                condition = expr(conditions[0].expr)
                if width(condition) != 1:
                    fail(statement, 'procedural if requires one-bit condition')
                yes = lower(statement.statement, env)
                no = lower(statement.elseClause.clause, env) if statement.elseClause else env
                return {name: add(statement, 'proc_mux', signals[name]['width'], [condition, yes[name], no[name]],
                                  signed=signals[name]['signed'])
                        if yes[name] != no[name] else yes[name] for name in env}
            fail(statement, 'unsupported clocked statement')
        next_state.update(lower(process.statement.statement, initial))
    for name, info in signals.items():
        if info['direction'] != 'input' and name not in assignments and name not in owners:
            fail(module, 'undriven signal ' + name)
    order, active, done = [], set(), set()

    def dependencies(i):
        node = ir_nodes[i]
        if node['op'] in ('ref', 'select'):
            yield node['name']
        for arg in node['args']:
            yield from dependencies(arg)

    def schedule(name):
        if name in active:
            fail(module, 'combinational cycle through ' + name)
        if name in done or signals[name]['direction'] == 'input' or name in owners:
            return
        active.add(name)
        for dep in dependencies(assignments[name]):
            schedule(dep)
        active.remove(name); done.add(name); order.append(name)
    for name in assignments:
        schedule(name)
    result = {'format': 'chisel2lean-fourstate-ir-v3', 'mode': mode, 'top': top, 'source': str(source),
            'signals': list(signals.values()), 'nodes': ir_nodes, 'assignments': assignments,
            'order': order, 'parser': 'pyslang 12.0.0'}
    if sequential:
        result.update(clock=clock,
                      states=[dict(signals[name], process=owners[name]) for name in owners], next_state=next_state)
    return result
