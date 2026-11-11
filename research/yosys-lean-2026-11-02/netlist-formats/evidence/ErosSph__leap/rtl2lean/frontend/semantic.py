"""Strict lowering from PySlang's elaborated semantic AST to rtl2lean AST."""
from __future__ import annotations

import re
from contextvars import ContextVar
from typing import Any
from .scopes import active_symbols, resolved_name, symbol_names

from .ast import (
    ArraySelect, BeginEnd, BinOp, BitLit, BitRange, BitSelect, BlockingAssign, CaseStmt,
    Concat, CondExpr, EventControl, Expr, Ident, IfStmt, IntLit, NoStmt,
    NonBlockingAssign, Process, ProcessType, Stmt, UnaryOp, LocalDecl, FunctionEval, IntegralCast,
    UnknownLit,
)


class UnsupportedConstructError(RuntimeError):
    """A construct has no semantics-preserving lowering yet."""


_function_stack = ContextVar("rtl2lean_function_stack", default=())
_static_loop_values = ContextVar("rtl2lean_static_loop_values", default={})
_nondet_serial = ContextVar("rtl2lean_nondet_serial", default=0)


def reset_nondet_serial():
    """Reset occurrence numbering for one elaborated top-level design."""
    return _nondet_serial.set(0)


def restore_nondet_serial(token) -> None:
    _nondet_serial.reset(token)


def unknown_literal(width: int, text: str = "'x") -> UnknownLit:
    """Lower X/Z/? bits to an explicit, quantified binary choice.

    Known digits in binary/octal/hex literals are retained exactly. Decimal
    unknowns and unbased unknown literals conservatively leave every bit free.
    """
    serial = _nondet_serial.get()
    _nondet_serial.set(serial + 1)
    known_mask = 0
    known_value = 0
    compact = text.replace("_", "").strip()
    match = re.search(r"'[sS]?([bBoOhHdD])([0-9a-fA-FxXzZ?]+)", compact)
    if match and match.group(1).lower() in {"b", "o", "h"}:
        radix = {"b": 1, "o": 3, "h": 4}[match.group(1).lower()]
        offset = 0
        for digit in reversed(match.group(2)):
            if digit not in "xXzZ?":
                value = int(digit, 16 if radix == 4 else 8 if radix == 3 else 2)
                digit_mask = (1 << radix) - 1
                known_mask |= digit_mask << offset
                known_value |= value << offset
            offset += radix
    mask = (1 << width) - 1
    return UnknownLit(
        width=width,
        known_mask=known_mask & mask,
        known_value=known_value & mask,
        choice_name=f"__rtl_nondet_{serial:04d}",
    )


def _typed_integer(value, typ):
    width = int(typ.bitWidth)
    value %= 1 << width
    return value - (1 << width) if typ.isSigned and value >= (1 << (width-1)) else value


def _loop_integer(expr):
    """Evaluate only explicit finite integer loop control, with Slang types."""
    if _suffix(expr.kind) == "Conversion":
        return _typed_integer(_loop_integer(expr.operand), expr.type)
    try:
        return _constant_expr_to_int(expr, "static loop control")
    except UnsupportedConstructError:
        pass
    if _suffix(expr.kind) == "BinaryOp":
        left, right = _loop_integer(expr.left), _loop_integer(expr.right)
        op = _suffix(expr.op)
        ops = {"Add":lambda:left+right, "Subtract":lambda:left-right,
               "LessThan":lambda:int(left<right), "LessThanEqual":lambda:int(left<=right),
               "GreaterThan":lambda:int(left>right), "GreaterThanEqual":lambda:int(left>=right),
               "Equality":lambda:int(left==right), "Inequality":lambda:int(left!=right)}
        if op in ops:
            return _typed_integer(ops[op](),expr.type)
    raise UnsupportedConstructError("procedural loop control is not statically evaluable")


def _build_static_loop(stmt):
    if stmt.loopVars or len(stmt.initializers)!=1 or len(stmt.steps)!=1:
        raise UnsupportedConstructError("procedural loop requires one explicit local induction variable")
    init, step = stmt.initializers[0], stmt.steps[0]
    if _suffix(init.kind)!="Assignment" or _suffix(step.kind)!="Assignment":
        raise UnsupportedConstructError("procedural loop requires assignment initializer and step")
    if _suffix(init.left.kind)!="NamedValue" or _suffix(step.left.kind)!="NamedValue":
        raise UnsupportedConstructError("procedural loop requires scalar induction variable")
    variable = init.left.symbol
    if step.left.symbol != variable or not variable.parentScope.isProceduralContext:
        raise UnsupportedConstructError("procedural loop induction variable must be a local temporary")
    if init.isNonBlocking or step.isNonBlocking:
        raise UnsupportedConstructError("nonblocking procedural loop control")
    def reject_counter_writes(node):
        if _suffix(node.kind)=="Assignment":
            lhs=node.left
            while _suffix(lhs.kind) in {"ElementSelect","RangeSelect"}:
                lhs=lhs.value
            if _suffix(lhs.kind)=="NamedValue" and lhs.symbol==variable:
                raise UnsupportedConstructError("procedural loop body modifies induction variable")
        if _suffix(node.kind)=="UnaryOp" and ("Increment" in str(node.op) or "Decrement" in str(node.op)):
            raise UnsupportedConstructError("procedural loop body has increment/decrement side effects")
    stmt.body.visit(reject_counter_writes)
    value = _typed_integer(_loop_integer(init.right),variable.type)
    width = int(variable.type.bitWidth)
    result = [BlockingAssign(Ident(resolved_name(variable)),BitLit(width,value % (1 << width)))]
    token = _static_loop_values.set(dict(_static_loop_values.get()))
    try:
        seen = set()
        while True:
            _static_loop_values.get()[variable] = value
            if not _loop_integer(stmt.stopExpr):
                break
            if value in seen or len(seen)>=4096:
                raise UnsupportedConstructError("procedural loop is nonterminating or exceeds static expansion limit")
            seen.add(value)
            result.append(build_stmt(stmt.body))
            value = _typed_integer(_loop_integer(step.right),variable.type)
            result.append(BlockingAssign(Ident(resolved_name(variable)),BitLit(width,value % (1 << width))))
        return BeginEnd(result)
    finally:
        _static_loop_values.reset(token)


def _build_pure_function(expr):
    callee = expr.subroutine
    if expr.isSystemCall or _suffix(expr.subroutineKind) != "Function":
        raise UnsupportedConstructError("UNSUPPORTED_CALL_SEMANTICS: only checked pure user functions are supported")
    if callee in _function_stack.get() or len(_function_stack.get()) >= 64:
        raise UnsupportedConstructError("UNSUPPORTED_CALL_SEMANTICS: recursive or excessively nested function")
    if "DPI" in str(callee.flags) or callee.body is None:
        raise UnsupportedConstructError("UNSUPPORTED_CALL_SEMANTICS: external/DPI function")
    if len(callee.arguments) != len(expr.arguments):
        raise UnsupportedConstructError("UNSUPPORTED_CALL_SEMANTICS: unresolved function arguments")
    if any(_suffix(a.direction) != "In" for a in callee.arguments):
        raise UnsupportedConstructError("UNSUPPORTED_CALL_SEMANTICS: output/ref function argument")
    actuals = [build_expr(a) for a in expr.arguments]
    variables = list(callee.arguments) + [callee.returnValVar]
    def inspect(node):
        k = _suffix(node.kind)
        if k == "VariableDeclaration":
            variables.append(node.symbol)
        if k in {"Timed", "Wait", "EventTrigger"}:
            raise UnsupportedConstructError("UNSUPPORTED_CALL_SEMANTICS: timed function body")
    callee.body.visit(inspect)
    variables = list(dict.fromkeys(variables))
    allowed = set(variables)
    def check_writes(node):
        if _suffix(node.kind) == "Assignment":
            target = node.left
            while _suffix(target.kind) in {"ElementSelect", "RangeSelect"}:
                target = target.value
            if _suffix(target.kind) != "NamedValue" or target.symbol not in allowed or node.isNonBlocking:
                raise UnsupportedConstructError("UNSUPPORTED_CALL_SEMANTICS: nonlocal/nonatomic function side effect")
        if _suffix(node.kind) == "UnaryOp" and ("Increment" in str(node.op) or "Decrement" in str(node.op)):
            raise UnsupportedConstructError("UNSUPPORTED_CALL_SEMANTICS: function increment side effect")
    callee.body.visit(check_writes)
    names = dict(symbol_names.get())
    widths = {}
    used = set(names.values())
    for variable in variables:
        if variable is None or variable.type.isUnpackedArray:
            raise UnsupportedConstructError("UNSUPPORTED_CALL_SEMANTICS: missing return or local-memory function")
        width = int(variable.type.bitWidth)
        if width <= 0:
            raise UnsupportedConstructError("UNSUPPORTED_CALL_SEMANTICS: non-integral function variable")
        idx = len(names)
        name = f"__fn_{idx}_{variable.name}"
        while name in used:
            idx += 1
            name = f"__fn_{idx}_{variable.name}"
        used.add(name)
        names[variable] = name
        widths[name] = width
    token = symbol_names.set(names)
    stack_token = _function_stack.set(_function_stack.get() + (callee,))
    try:
        body = build_stmt(callee.body)
        arguments = [(names[formal],int(formal.type.bitWidth),actual) for formal,actual in zip(callee.arguments,actuals)]
        return FunctionEval(arguments,widths,body,names[callee.returnValVar],int(callee.returnType.bitWidth))
    finally:
        _function_stack.reset(stack_token)
        symbol_names.reset(token)


def _suffix(value: Any) -> str:
    return str(value).rsplit(".", 1)[-1]


def _width(expr: Any) -> int:
    expr_type = getattr(expr, "type", None)
    width = getattr(expr_type, "bitWidth", 32)
    width = width() if callable(width) else width
    try:
        return max(1, int(width))
    except (TypeError, ValueError):
        return 32


def _constant_to_int(value: Any, context: str) -> int:
    if value is None:
        raise UnsupportedConstructError(f"non-constant {context}")
    # ConstantValue may wrap SVInt. Reject unspecified bits before conversion;
    # SVInt implements int(), while its display text is Verilog, not Python.
    value = getattr(value, "value", value)
    unknown = getattr(value, "hasUnknown", False)
    if unknown() if callable(unknown) else unknown:
        raise UnsupportedConstructError(f"X/Z constant in {context}: {value}")
    if hasattr(value, "__int__"):
        try:
            return int(value)
        except (TypeError, ValueError, RuntimeError):
            pass
    if hasattr(value, "convertToInt"):
        try:
            converted = value.convertToInt()
            inner = getattr(converted, "value", converted)
            return int(inner)
        except (TypeError, ValueError, RuntimeError):
            pass
    try:
        return int(str(value), 0)
    except ValueError:
        raise UnsupportedConstructError(f"cannot lower constant {value!s} in {context}") from None


def _constant_expr_to_int(expr: Any, context: str) -> int:
    symbol = getattr(expr, "symbol", None)
    if symbol in _static_loop_values.get():
        return _static_loop_values.get()[symbol]
    value = getattr(expr, "constant", None)
    if value is None and "IntegerLiteral" in _suffix(getattr(expr, "kind", "")):
        value = expr.value
    if value is None:
        symbol = getattr(expr, "symbol", None)
        value = getattr(symbol, "value", None)
    return _constant_to_int(value, context)


def _packed_bounds(value_expr: Any) -> tuple[int, int] | None:
    """Return the declared packed bounds that define SV bit numbering.

    A packed vector's right bound is always its least-significant storage bit.
    Keeping this mapping matters for declarations such as ``logic [1:6]``:
    declaration index 1 denotes offset 5, not LSB offset 1.
    """
    value_type = getattr(value_expr, "type", None)
    is_unpacked = getattr(value_type, "isUnpackedArray", False)
    is_unpacked = is_unpacked() if callable(is_unpacked) else is_unpacked
    packed_range = None if is_unpacked else getattr(value_type, "range", None)
    if packed_range is None:
        return None
    try:
        left = int(packed_range.left)
        right = int(packed_range.right)
        range_width = int(packed_range.width)
        bit_width = int(value_type.bitWidth)
    except (AttributeError, TypeError, ValueError):
        return None
    # A multidimensional packed array needs one mapping per dimension.  Do not
    # silently apply the one-dimensional rule to it.
    if range_width != bit_width:
        raise UnsupportedConstructError(
            "multidimensional packed selection requires explicit dimension lowering"
        )
    return left, right


def _packed_index(index_expr: Any, value_expr: Any) -> Expr:
    """Map an SV declaration index to the flat BitVec LSB offset."""
    index = build_expr(index_expr)
    bounds = _packed_bounds(value_expr)
    if bounds is None:
        return index
    left, right = bounds
    try:
        declared_index = _constant_expr_to_int(index_expr, "packed index")
    except UnsupportedConstructError:
        right_lit = BitLit(_width(index_expr), right)
        return (
            BinOp("-", index, right_lit)
            if left >= right
            else BinOp("-", right_lit, index)
        )
    offset = declared_index - right if left >= right else right - declared_index
    return BitLit(_width(index_expr), offset)


UNARY_OPERATORS = {
    "Plus": "+",
    "Minus": "-",
    "LogicalNot": "!",
    "BitwiseNot": "~",
    "BitwiseAnd": "&",
    "BitwiseOr": "|",
    "BitwiseXor": "^",
    "BitwiseNand": "~&",
    "BitwiseNor": "~|",
    "BitwiseXnor": "~^",
}


BINARY_OPERATORS = {
    "Add": "+",
    "Subtract": "-",
    "Multiply": "*",
    "Divide": "/",
    "Mod": "%",
    "BinaryAnd": "&",
    "BinaryOr": "|",
    "BinaryXor": "^",
    "BinaryXnor": "~^",
    "LogicalAnd": "&&",
    "LogicalOr": "||",
    "Equality": "==",
    "Inequality": "!=",
    "CaseEquality": "===",
    "CaseInequality": "!==",
    "LessThan": "<",
    "LessThanEqual": "<=",
    "GreaterThan": ">",
    "GreaterThanEqual": ">=",
    "LogicalShiftLeft": "<<",
    "LogicalShiftRight": ">>",
    "ArithmeticShiftLeft": "<<<",
    "ArithmeticShiftRight": ">>>",
}


def build_expr(expr: Any) -> Expr:
    if expr is None:
        raise UnsupportedConstructError("missing expression")
    kind = _suffix(getattr(expr, "kind", ""))

    if kind == "StringLiteral":
        # Packed strings are fixed integral constants in synthesizable data
        # expressions. Preserve their elaborated width and value exactly.
        if expr.type.isIntegral:
            width = _width(expr)
            return BitLit(width, _constant_expr_to_int(expr, "packed string literal") % (1 << width))

    if "IntegerLiteral" in kind:
        width = _width(expr)
        value = getattr(expr, "value", None)
        raw = getattr(value, "value", value)
        has_unknown = getattr(raw, "hasUnknown", False)
        if has_unknown() if callable(has_unknown) else has_unknown:
            return unknown_literal(width, str(raw))
        return BitLit(width, _constant_expr_to_int(expr, kind) % (1 << width))

    if kind == "Call":
        # These system functions are pure integral casts/sizing operations;
        # their semantics do not depend on an RTL simulator or an X policy.
        if expr.isSystemCall and str(expr.subroutineName) in {"$signed", "$unsigned"}:
            if len(expr.arguments) != 1 or not expr.arguments[0].type.isIntegral:
                raise UnsupportedConstructError("integral sign cast requires one integral argument")
            return IntegralCast(build_expr(expr.arguments[0]), _width(expr), str(expr.subroutineName) == "$signed")
        if expr.isSystemCall and str(expr.subroutineName) in {"$clog2", "$bits"}:
            # Only elaboration-constant, pure sizing functions. Never fold
            # runtime system calls such as $time/$random into a constant.
            width = _width(expr)
            return BitLit(width, _constant_expr_to_int(expr, "constant sizing system function") % (1 << width))
        return _build_pure_function(expr)

    if kind == "NamedValue":
        symbol = getattr(expr, "symbol", None)
        if symbol is None:
            raise UnsupportedConstructError("NamedValue without symbol")
        symbol_kind = _suffix(getattr(symbol, "kind", ""))
        if symbol in _static_loop_values.get():
            width = _width(expr)
            return BitLit(width,_static_loop_values.get()[symbol] % (1 << width))
        if symbol_kind in {"Parameter", "EnumValue"}:
            return BitLit(
                _width(expr),
                _constant_to_int(getattr(symbol, "value", None), str(symbol.name)),
            )
        return Ident(resolved_name(symbol))

    if kind == "UnaryOp":
        operator = UNARY_OPERATORS.get(_suffix(expr.op))
        if operator is None:
            raise UnsupportedConstructError(f"unsupported unary operator: {expr.op}")
        return UnaryOp(operator, build_expr(expr.operand))

    if kind == "BinaryOp":
        operator = BINARY_OPERATORS.get(_suffix(expr.op))
        if operator is None:
            raise UnsupportedConstructError(f"unsupported binary operator: {expr.op}")
        if operator in {"===", "!=="}:
            raise UnsupportedConstructError(f"case equality is unsupported in the binary formal model: {operator}")
        if operator in {"<", ">", "<=", ">="} and expr.left.type.isSigned and expr.right.type.isSigned:
            width = max(_width(expr.left), _width(expr.right))
            # Flipping the common-width sign bit maps signed ordering to
            # unsigned ordering, using existing bitvector IR operators.
            mask = BitLit(width, 1 << (width-1))
            return BinOp(operator,
                BinOp("^", IntegralCast(build_expr(expr.left), width, True), mask),
                BinOp("^", IntegralCast(build_expr(expr.right), width, True), mask))
        return BinOp(operator, build_expr(expr.left), build_expr(expr.right))

    if kind == "Conversion":
        return build_expr(expr.operand)

    if kind == "ElementSelect":
        value_type = getattr(expr.value, "type", None)
        is_unpacked = getattr(value_type, "isUnpackedArray", False)
        is_unpacked = is_unpacked() if callable(is_unpacked) else is_unpacked
        if is_unpacked:
            array_range = value_type.range
            element_width = int(value_type.elementType.bitWidth)
            return ArraySelect(
                build_expr(expr.value),
                build_expr(expr.selector),
                element_width,
                int(array_range.left),
                int(array_range.right),
            )
        return BitSelect(build_expr(expr.value), _packed_index(expr.selector, expr.value))

    if kind in {"RangeSelect", "PartSelect"}:
        left = getattr(expr, "left", None)
        right = getattr(expr, "right", None)
        if left is None or right is None:
            selection = getattr(expr, "selectionRange", None)
            left = getattr(selection, "left", None)
            right = getattr(selection, "right", None)
        if left is None or right is None:
            raise UnsupportedConstructError(f"unsupported range select shape: {expr}")
        return BitRange(
            build_expr(expr.value),
            _packed_index(left, expr.value),
            _packed_index(right, expr.value),
        )

    if kind == "Concatenation":
        return Concat([build_expr(operand) for operand in expr.operands])

    if kind == "Replication":
        count = _constant_expr_to_int(expr.count, "replication count")
        if count < 0 or count > 4096:
            raise UnsupportedConstructError(f"unsupported replication count: {count}")
        repeated = [build_expr(operand) for operand in expr.concat.operands]
        return Concat(repeated * count)

    if kind == "ConditionalOp":
        conditions = getattr(expr, "conditions", ())
        if len(conditions) != 1 or getattr(conditions[0], "pattern", None) is not None:
            raise UnsupportedConstructError("pattern conditional expression")
        return CondExpr(
            build_expr(conditions[0].expr),
            build_expr(expr.left),
            build_expr(expr.right),
        )

    if kind == "Assignment":
        raise UnsupportedConstructError("assignment expression used as a value")

    raise UnsupportedConstructError(f"unsupported expression kind: {kind}")


def _statement_body(stmt: Any) -> Any:
    kind = _suffix(getattr(stmt, "kind", ""))
    if kind == "Timed":
        return stmt.stmt
    if kind == "Block":
        return stmt.body
    return stmt


def build_stmt(stmt: Any) -> Stmt:
    if stmt is None:
        return NoStmt()
    stmt = _statement_body(stmt)
    kind = _suffix(getattr(stmt, "kind", ""))

    if kind == "List":
        return BeginEnd([build_stmt(item) for item in stmt.list])
    if kind == "Block":
        return build_stmt(stmt.body)
    if kind == "Empty":
        return NoStmt()
    if kind == "VariableDeclaration":
        variable = stmt.symbol
        if variable.type.isUnpackedArray:
            raise UnsupportedConstructError("procedural local arrays require explicit local-memory semantics")
        initializer = variable.initializer
        if initializer is not None and _suffix(variable.lifetime) != "Automatic":
            raise UnsupportedConstructError("static local initialization requires persistent lifetime semantics")
        return LocalDecl(resolved_name(variable), int(variable.type.bitWidth),
                         build_expr(initializer) if initializer is not None else None)
    if kind == "ExpressionStatement":
        expr = stmt.expr
        if _suffix(expr.kind) == "Call":
            def empty_task_body(body):
                if body is None:
                    return False
                tag = _suffix(body.kind)
                if tag == "Empty":
                    return True
                if tag == "Block":
                    return empty_task_body(body.body)
                if tag == "List":
                    return all(empty_task_body(item) for item in body.list)
                return False
            if (not expr.isSystemCall and _suffix(expr.subroutineKind) == "Task"
                    and not expr.arguments and not expr.subroutine.arguments
                    and "DPI" not in str(expr.subroutine.flags)
                    and empty_task_body(expr.subroutine.body)):
                return NoStmt()
        if _suffix(getattr(expr, "kind", "")) != "Assignment":
            raise UnsupportedConstructError(
                f"unsupported procedural expression: {getattr(expr, 'kind', '')}"
            )
        target = build_expr(expr.left)
        value = build_expr(expr.right)
        return (
            NonBlockingAssign(target, value)
            if bool(expr.isNonBlocking)
            else BlockingAssign(target, value)
        )
    if kind == "Conditional":
        conditions = stmt.conditions
        if len(conditions) != 1 or conditions[0].pattern is not None:
            raise UnsupportedConstructError("pattern condition in if statement")
        predicate = conditions[0].expr
        # Slang's known binary constant predicate selects a procedural
        # branch just as it does at runtime. Do not lower unreachable actions
        # (e.g. disabled diagnostics) or materialize compile-time string tests.
        # Unknown / dynamic predicates still use the ordinary conditional IR.
        try:
            constant = _constant_expr_to_int(conditions[0].expr, "procedural condition")
        except UnsupportedConstructError:
            constant = None
        if constant is not None:
            return build_stmt(stmt.ifTrue if constant else stmt.ifFalse)
        return IfStmt(
            build_expr(conditions[0].expr),
            build_stmt(stmt.ifTrue),
            build_stmt(stmt.ifFalse) if stmt.ifFalse is not None else None,
        )
    if kind == "Case":
        cases = []
        for item in stmt.items:
            for expression in item.expressions:
                cases.append((build_expr(expression), build_stmt(item.stmt)))
        default_stmt = build_stmt(stmt.defaultCase) if stmt.defaultCase is not None else None
        return CaseStmt(build_expr(stmt.expr), cases, default_stmt)
    if kind == "ForLoop":
        return _build_static_loop(stmt)
    if kind in {"WhileLoop", "DoWhileLoop", "Forever"}:
        raise UnsupportedConstructError(f"procedural loop is unsupported: {kind}")

    raise UnsupportedConstructError(f"unsupported statement kind: {kind}")


def build_process(symbol: Any) -> Process:
    procedure = _suffix(symbol.procedureKind)
    process_types = {
        "Always": ProcessType.Always,
        "AlwaysComb": ProcessType.AlwaysComb,
        "AlwaysFF": ProcessType.AlwaysFF,
        "AlwaysLatch": ProcessType.AlwaysLatch,
        "Initial": ProcessType.Initial,
        "Final": ProcessType.Initial,
    }
    if procedure not in process_types:
        raise UnsupportedConstructError(f"unsupported procedural block: {symbol.procedureKind}")
    if procedure in {"AlwaysLatch", "Final"}:
        raise UnsupportedConstructError(f"unsupported procedural block: {procedure}")

    text = str(symbol.syntax)
    events = [
        (edge.lower(), signal)
        for edge, signal in re.findall(
            r"\b(posedge|negedge)\s+([A-Za-z_$][\w$]*)", text
        )
    ]
    events = [(edge, resolved_name(symbol.parentScope.lookupName(signal))) for edge, signal in events]
    event_control = EventControl(events) if events else None
    # Anonymous nested scopes can have identical hierarchicalPath strings.
    # Slang symbol identity, not spelling, distinguishes shadowed temporaries.
    names = dict(symbol_names.get())
    used = set(names.values())
    locals_ = set()
    def collect_local(node):
        if _suffix(node.kind) == "VariableDeclaration":
            variable = node.symbol
            locals_.add(variable)
            index = len(names)
            name = f"__local_{index}_{variable.name}"
            while name in used:
                index += 1
                name = f"__local_{index}_{variable.name}"
            names[variable] = name
            used.add(name)
    symbol.body.visit(collect_local)
    token = symbol_names.set(names)
    try:
        state_names = set()
        def targets(expr):
            tag = _suffix(expr.kind)
            if tag in {"ElementSelect", "RangeSelect"}:
                yield from targets(expr.value)
            elif tag == "Concatenation":
                for item in expr.operands:
                    yield from targets(item)
            else:
                target = getattr(expr, "symbol", None)
                if target is not None and target not in locals_:
                    yield resolved_name(target)
        def assigned(node):
            if _suffix(node.kind) == "Assignment":
                state_names.update(targets(node.left))
        if procedure in {"AlwaysFF", "Initial"} or (event_control and event_control.is_sequential):
            symbol.body.visit(assigned)
        return Process(proc_type=process_types[procedure], event_control=event_control,
                       body=build_stmt(symbol.body),
                       state_targets=[Ident(name) for name in sorted(state_names)])
    finally:
        symbol_names.reset(token)


def initial_is_inactive(stmt: Any) -> bool:
    """Prove that a parameter-specialized initial process performs no action.

    Do not traverse unreachable branches: a memory initializer under a false
    elaboration constant has no initial-state effect. Active actions and dynamic
    guards are rejected explicitly until initial-state lowering supports them.
    """
    if stmt is None:
        return True
    kind = _suffix(stmt.kind)
    if kind == "Empty":
        return True
    if kind == "Block":
        return initial_is_inactive(stmt.body)
    if kind == "List":
        return all(initial_is_inactive(s) for s in stmt.list)
    if kind == "Conditional" and len(stmt.conditions) == 1:
        cond = stmt.conditions[0]
        if cond.pattern is not None:
            return False
        try:
            value = _constant_expr_to_int(cond.expr, "initial guard")
        except UnsupportedConstructError:
            return False
        return initial_is_inactive(stmt.ifTrue if value else stmt.ifFalse)
    return False
