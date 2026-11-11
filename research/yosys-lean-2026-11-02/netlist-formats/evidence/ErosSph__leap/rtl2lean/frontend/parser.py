"""
PySlang 解析器 - 使用 pyslang v10 的 Compilation API 解析 SystemVerilog
"""

from __future__ import annotations

import logging
from dataclasses import fields, is_dataclass
import re
from pathlib import Path
from typing import Optional

import pyslang

from .ast import (
    ArraySelect, BeginEnd, BinOp, BitLit, BitRange, BitSelect, BitWidth, BlockingAssign,
    CallExpr, CaseStmt, Concat, CondExpr, ContinuousAssign, DataType,
    Declaration, EventControl, Expr, ForLoop, ForeverLoop, Ident, IfStmt,
    IntLit, Module, ModuleInstance, NoStmt, NonBlockingAssign, Parameter,
    Port, PortDirection, Process, ProcessType, Stmt, UnaryOp, WaitStmt,
    WhileLoop,
)
from .design import (
    DesignInfo, ElaboratedDesign, PortInfo, load_design, load_design_source,
)
from .semantic import (
    UnsupportedConstructError, build_expr, build_process, initial_is_inactive,
    reset_nondet_serial, restore_nondet_serial, unknown_literal,
)
from .scopes import active_symbols, flat_path, relative_path, resolved_name, symbol_names

logger = logging.getLogger(__name__)


# ───────────────────────── 数据类型映射 ─────────────────────────

def _sv_type_to_data_type(type_str: str) -> tuple[DataType, Optional[BitWidth]]:
    """从 SystemVerilog 类型字符串解析数据类型和位宽"""
    t = type_str.strip().lower()

    # 提取位宽
    bw = None
    m = re.search(r"\[(\d+)\s*:\s*(\d+)\]", t)
    if m:
        bw = BitWidth(int(m.group(1)), int(m.group(2)))

    # 确定基础类型 - 按优先级匹配
    # 先检查方向关键字
    has_output = "output" in t
    has_input = "input" in t

    # 提取类型关键字（去除方向和位宽）
    base = re.sub(r"\[.*?\]", "", t).strip()
    base = re.sub(r"(input|output|inout)\s*", "", base).strip()

    type_map = {
        "logic": DataType.Logic,
        "wire": DataType.Wire,
        "reg": DataType.Reg,
        "integer": DataType.Integer,
        "bit": DataType.Bit,
    }
    dt = type_map.get(base, DataType.Logic)

    # 如果有位宽，转换为向量类型
    if bw:
        vec_map = {
            DataType.Logic: DataType.LogicVector,
            DataType.Wire: DataType.WireVector,
            DataType.Reg: DataType.RegVector,
            DataType.Bit: DataType.BitVector,
        }
        dt = vec_map.get(dt, DataType.LogicVector)

    return dt, bw


def _pyslang_direction(dir_val) -> PortDirection:
    """将 pyslang 端口方向转换为我们的枚举"""
    dir_str = str(dir_val)
    if "Out" in dir_str:
        return PortDirection.Output
    if "Inout" in dir_str:
        return PortDirection.Inout
    return PortDirection.Input


# ───────────────────────── 表达式解析 ─────────────────────────

class _ExprBuilder:
    """从 pyslang 表达式符号构建 Expr"""

    @staticmethod
    def build(expr) -> Expr:
        if expr is None:
            return IntLit(0)

        kind = str(getattr(expr, "kind", ""))

        # 字面量
        if "IntegerLiteral" in kind or "TimeLiteral" in kind:
            return _ExprBuilder._build_literal(expr)

        # 标识符
        if "NamedValue" in kind:
            return Ident(expr.name)

        # 一元运算
        if "Unary" in kind:
            return _ExprBuilder._build_unary(expr)

        # 二元运算
        if "Binary" in kind:
            return _ExprBuilder._build_binary(expr)

        # 元素选择 (位选择/范围)
        if "ElementSelect" in kind:
            return _ExprBuilder._build_select(expr)

        # 拼接
        if "Concatenation" in kind:
            return _ExprBuilder._build_concat(expr)

        # 条件
        if "Conditional" in kind:
            return _ExprBuilder._build_conditional(expr)

        # 调用
        if "Call" in kind:
            return _ExprBuilder._build_call(expr)

        # 转换/类型转换
        if "Conversion" in kind or "Cast" in kind:
            operand = getattr(expr, "operand", None)
            if operand:
                return _ExprBuilder.build(operand)

        # 成员访问
        if "MemberAccess" in kind:
            return _ExprBuilder._build_member_access(expr)

        # 回退
        return _ExprBuilder._fallback(expr)

    @staticmethod
    def _build_literal(expr) -> Expr:
        """构建字面量"""
        val = getattr(expr, "constant", None)
        if val is not None:
            try:
                v = int(val)
                return IntLit(v)
            except (ValueError, TypeError):
                pass

        # 尝试从值获取
        if hasattr(expr, "getValue"):
            try:
                cv = expr.getValue()
                if hasattr(cv, "integer"):
                    return IntLit(int(cv.integer()))
            except:
                pass

        text = str(expr)
        try:
            return IntLit(int(text))
        except ValueError:
            return Ident(text)

    @staticmethod
    def _build_unary(expr) -> Expr:
        op_map = {
            "UnaryNot": "!", "UnaryNeg": "-", "UnaryBitwiseAnd": "&",
            "UnaryBitwiseOr": "|", "UnaryBitwiseXor": "^",
            "UnaryBitwiseNand": "~&", "UnaryBitwiseNor": "~|",
            "UnaryBitwiseXnor": "~^", "UnaryComplement": "~",
        }
        op = op_map.get(str(expr.kind), "?")
        operand = getattr(expr, "operand", None)
        return UnaryOp(op, _ExprBuilder.build(operand))

    @staticmethod
    def _build_binary(expr) -> Expr:
        op_map = {
            "Add": "+", "Subtract": "-", "Multiply": "*", "Divide": "/",
            "Mod": "%", "BinaryAnd": "&", "BinaryOr": "|", "BinaryXor": "^",
            "Equality": "==", "Inequality": "!=",
            "LessThan": "<", "GreaterThan": ">",
            "LessThanEqual": "<=", "GreaterThanEqual": ">=",
            "ShiftLeft": "<<", "ShiftRight": ">>",
            "LogicalAnd": "&&", "LogicalOr": "||",
            "BinaryXnor": "^~", "Power": "**",
        }
        op = op_map.get(str(expr.kind), "?")
        left = getattr(expr, "left", None)
        right = getattr(expr, "right", None)
        return BinOp(op, _ExprBuilder.build(left), _ExprBuilder.build(right))

    @staticmethod
    def _build_select(expr) -> Expr:
        value = getattr(expr, "value", None)
        selector = getattr(expr, "selector", None)
        if value and selector:
            return BitSelect(_ExprBuilder.build(value), _ExprBuilder.build(selector))
        return Ident("unknown")

    @staticmethod
    def _build_concat(expr) -> Expr:
        operands = []
        if hasattr(expr, "operands"):
            for op in expr.operands:
                operands.append(_ExprBuilder.build(op))
        return Concat(operands)

    @staticmethod
    def _build_conditional(expr) -> Expr:
        cond = getattr(expr, "cond", None)
        left = getattr(expr, "left", None)
        right = getattr(expr, "right", None)
        if cond and left and right:
            return CondExpr(
                _ExprBuilder.build(cond),
                _ExprBuilder.build(left),
                _ExprBuilder.build(right),
            )
        return IntLit(0)

    @staticmethod
    def _build_call(expr) -> CallExpr:
        name = "unknown"
        args = []
        if hasattr(expr, "name"):
            name = str(expr.name)
        if hasattr(expr, "arguments"):
            args = [_ExprBuilder.build(a) for a in expr.arguments]
        return CallExpr(name, args)

    @staticmethod
    def _build_member_access(expr) -> Expr:
        value = getattr(expr, "value", None)
        member = getattr(expr, "member", None)
        if member:
            return Ident(str(member))
        return Ident("unknown")

    @staticmethod
    def _fallback(expr) -> Expr:
        text = str(expr).strip()
        if text.isidentifier():
            return Ident(text)
        try:
            return IntLit(int(text))
        except ValueError:
            return Ident(text) if text else IntLit(0)


# ───────────────────────── 语句解析 ─────────────────────────

class _StmtBuilder:
    """从 pyslang 语句符号构建 Stmt"""

    @staticmethod
    def build(stmt) -> Stmt:
        if stmt is None:
            return NoStmt()

        kind = str(getattr(stmt, "kind", ""))

        # 阻塞赋值
        if "BlockingAssignment" in kind:
            return _StmtBuilder._build_blocking(stmt)

        # 非阻塞赋值
        if "Nonblocking" in kind:
            return _StmtBuilder._build_nonblocking(stmt)

        # 条件语句
        if "Conditional" in kind:
            return _StmtBuilder._build_if(stmt)

        # Case 语句
        if "Case" in kind:
            return _StmtBuilder._build_case(stmt)

        # For 循环
        if "ForLoop" in kind:
            return _StmtBuilder._build_for(stmt)

        # While / Do-While
        if "While" in kind or "DoWhile" in kind:
            return _StmtBuilder._build_while(stmt)

        # Forever
        if "Forever" in kind:
            return _StmtBuilder._build_forever(stmt)

        # 块语句
        if "Block" in kind:
            return _StmtBuilder._build_block(stmt)

        # Timed / 带时序控制的语句
        if "Timed" in kind:
            inner = getattr(stmt, "stmt", None)
            return _StmtBuilder.build(inner)

        # 空语句
        if "Empty" in kind:
            return NoStmt()

        # 表达式语句
        if "Expression" in kind:
            expr = getattr(stmt, "expr", None)
            return ExprStmt(_ExprBuilder.build(expr))

        # 过程 assign/deassign
        if "ProceduralAssign" in kind:
            return _StmtBuilder._build_procedural_assign(stmt)

        # 过程 deassign
        if "ProceduralDeassign" in kind:
            return NoStmt()

        return NoStmt()

    @staticmethod
    def _build_blocking(stmt) -> BlockingAssign:
        left = getattr(stmt, "left", None)
        right = getattr(stmt, "right", None)
        return BlockingAssign(_ExprBuilder.build(left), _ExprBuilder.build(right))

    @staticmethod
    def _build_nonblocking(stmt) -> NonBlockingAssign:
        left = getattr(stmt, "left", None)
        right = getattr(stmt, "right", None)
        return NonBlockingAssign(_ExprBuilder.build(left), _ExprBuilder.build(right))

    @staticmethod
    def _build_if(stmt) -> IfStmt:
        cond = getattr(stmt, "cond", None)
        if_true = getattr(stmt, "ifTrue", None)
        if_false = getattr(stmt, "ifFalse", None)
        return IfStmt(
            _ExprBuilder.build(cond),
            _StmtBuilder.build(if_true),
            _StmtBuilder.build(if_false) if if_false else None,
        )

    @staticmethod
    def _build_case(stmt) -> CaseStmt:
        cond = getattr(stmt, "expr", None)
        cases = []
        default_stmt = None

        if hasattr(stmt, "items"):
            for item in stmt.items:
                item_kind = str(getattr(item, "kind", ""))
                if "Default" in item_kind:
                    default_stmt = _StmtBuilder.build(getattr(item, "stmt", None))
                else:
                    expressions = getattr(item, "expressions", [])
                    item_stmt = getattr(item, "stmt", None)
                    for expr_node in expressions:
                        pattern = _ExprBuilder.build(expr_node)
                        cases.append((pattern, _StmtBuilder.build(item_stmt)))

        return CaseStmt(_ExprBuilder.build(cond), cases, default_stmt)

    @staticmethod
    def _build_for(stmt) -> ForLoop:
        init = getattr(stmt, "init", None)
        cond = getattr(stmt, "cond", None)
        update = getattr(stmt, "update", None)
        body = getattr(stmt, "stmt", None)
        return ForLoop(
            _StmtBuilder.build(init),
            _ExprBuilder.build(cond),
            _StmtBuilder.build(update),
            _StmtBuilder.build(body),
        )

    @staticmethod
    def _build_while(stmt) -> WhileLoop:
        cond = getattr(stmt, "cond", None)
        body = getattr(stmt, "stmt", None)
        return WhileLoop(_ExprBuilder.build(cond), _StmtBuilder.build(body))

    @staticmethod
    def _build_forever(stmt) -> ForeverLoop:
        body = getattr(stmt, "stmt", None)
        return ForeverLoop(_StmtBuilder.build(body))

    @staticmethod
    def _build_block(stmt) -> BeginEnd:
        stmts = []
        if hasattr(stmt, "members"):
            for m in stmt.members:
                stmts.append(_StmtBuilder.build(m))
        return BeginEnd(stmts)

    @staticmethod
    def _build_procedural_assign(stmt):
        expr = getattr(stmt, "expr", None)
        if expr:
            return _StmtBuilder.build(expr)
        return NoStmt()


# ───────────────────────── 主解析器 ─────────────────────────

class SVParser:
    """
    使用 PySlang v10 解析 SystemVerilog/Verilog 源代码。

    使用 Compilation API 进行语义分析，获取完整的类型信息。
    """

    def __init__(self):
        self._errors: list[str] = []
        self._warnings: list[str] = []

    @property
    def errors(self) -> list[str]:
        return list(self._errors)

    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    def parse_file(self, path: str | Path) -> list[Module]:
        """从文件解析"""
        return self.parse_files([path])

    def parse_files(
        self,
        paths: list[str | Path],
        top_module: Optional[str] = None,
        unified_compilation_unit: bool = False,
    ) -> list[Module]:
        """Parse one elaborated design from a complete RTL source set."""
        resolved = [Path(path) for path in paths]
        for path in resolved:
            if not path.exists():
                raise FileNotFoundError(f"文件不存在: {path}")
        logger.info("解析文件集: %s", ", ".join(str(path) for path in resolved))
        design = load_design(resolved, top_module, unified_compilation_unit)
        return [self.parse_design(design)]

    def parse_design(self, design: ElaboratedDesign) -> Module:
        """Lower an already elaborated shared design into the compiler AST."""
        self._warnings.extend(design.info.warnings)
        nondet_token = reset_nondet_serial()
        try:
            module = self._parse_instance(design.instance, design.info)
        finally:
            restore_nondet_serial(nondet_token)
        return module

    def parse_source(self, source: str) -> list[Module]:
        """从字符串解析"""
        logger.info("解析源代码字符串")
        design = load_design_source(source)
        return [self.parse_design(design)]

    def _parse_tree(self, tree) -> list[Module]:
        """解析语法树"""
        # 收集语法错误
        for diag in tree.diagnostics:
            msg = str(diag)
            if diag.severity == pyslang.DiagnosticSeverity.Error:
                self._errors.append(msg)
            elif diag.severity == pyslang.DiagnosticSeverity.Warning:
                self._warnings.append(msg)

        # 使用 Compilation 进行语义分析
        comp = pyslang.Compilation()
        comp.addSyntaxTree(tree)

        # 收集语义错误
        for diag in comp.getSemanticDiagnostics():
            msg = str(diag)
            if diag.severity == pyslang.DiagnosticSeverity.Error:
                self._errors.append(msg)
            elif diag.severity == pyslang.DiagnosticSeverity.Warning:
                self._warnings.append(msg)

        # 提取模块
        modules = []
        root = comp.getRoot()
        for inst in root.topInstances:
            try:
                mod = self._parse_instance(inst)
                modules.append(mod)
            except Exception as e:
                self._errors.append(f"解析实例失败: {e}")
                logger.exception("解析实例失败")

        return modules

    def _parse_instance(self, inst, design_info: Optional[DesignInfo] = None) -> Module:
        symbols = list(active_symbols(inst.body))
        names = {
            str(s.hierarchicalPath): flat_path(relative_path(s, inst.body))
            for s in symbols if str(s.kind).rsplit(".", 1)[-1] in {"Variable", "Net", "Instance"}
        }
        if len(set(names.values())) != len(names):
            raise UnsupportedConstructError("hierarchy flattened identifier collision")
        token = symbol_names.set(names)
        try:
            return self._parse_instance_scoped(inst, design_info, symbols)
        finally:
            symbol_names.reset(token)

    def _parse_instance_scoped(self, inst, design_info, symbols) -> Module:
        """解析实例为 Module"""
        body = inst.body
        name = body.name

        logger.info(f"解析模块: {name}")

        # 从语法树提取端口类型信息
        port_type_map = {}
        semantic_ports = {
            port.name: port for port in design_info.ports
        } if design_info else {}
        if design_info is None:
            from .design import _extract_ports
            semantic_ports = {port.name: port for port in _extract_ports(body)}

        # 解析端口
        ports = []
        for port_sym in body.portList:
            port = self._parse_port_symbol(
                port_sym,
                port_type_map,
                semantic_ports.get(str(port_sym.name)),
            )
            if port:
                ports.append(port)

        # Parse the elaborated semantic symbols. Syntax-only parsing loses the
        # meaning of non-ANSI declarations and wraps assignments in generic
        # expression statements, which previously caused silent empty bodies.
        declarations: list[Declaration] = []
        continuous_assigns: list[ContinuousAssign] = []
        processes: list[Process] = []
        instances: list[ModuleInstance] = []
        port_names = {port.name for port in ports}
        # Reserve every local name before flattening, including declarations
        # textually after an instance. Otherwise flattening can alias real state.
        local_names = port_names | {
            resolved_name(symbol) for symbol in symbols
            if str(getattr(symbol, "kind", "")).rsplit(".", 1)[-1] in {"Variable", "Net"}
        }
        for symbol in symbols:
            kind = str(getattr(symbol, "kind", "")).rsplit(".", 1)[-1]
            if kind in {"Variable", "Net"} and resolved_name(symbol) not in port_names:
                is_unpacked = getattr(symbol.type, "isUnpackedArray", False)
                is_unpacked = is_unpacked() if callable(is_unpacked) else is_unpacked
                if is_unpacked:
                    array_range = symbol.type.range
                    element_width = int(symbol.type.elementType.bitWidth)
                    left = int(array_range.left)
                    right = int(array_range.right)
                    width = element_width * int(array_range.width)
                else:
                    element_width = None
                    left = None
                    right = None
                    width = int(symbol.type.bitWidth)
                bit_width = BitWidth(width - 1, 0) if width > 1 else None
                if kind == "Variable":
                    data_type = DataType.RegVector if width > 1 else DataType.Reg
                    decl_kind = "reg"
                else:
                    data_type = DataType.WireVector if width > 1 else DataType.Wire
                    decl_kind = "wire"
                declarations.append(Declaration(
                    kind=decl_kind,
                    data_type=data_type,
                    names=[resolved_name(symbol)],
                    bit_width=bit_width,
                    unpacked_element_width=element_width,
                    unpacked_left=left,
                    unpacked_right=right,
                    signed=bool(symbol.type.isSigned),
                ))
                # A net declaration assignment (`wire x = expr`) has
                # continuous-assignment semantics.  Keeping only the net
                # declaration silently leaves x at the model's initial value.
                initializer = getattr(symbol, "initializer", None)
                if kind == "Net" and initializer is not None:
                    continuous_assigns.append(ContinuousAssign(
                        target=Ident(resolved_name(symbol)),
                        value=build_expr(initializer),
                    ))
            elif kind == "ContinuousAssign":
                assignment = symbol.assignment
                continuous_assigns.append(ContinuousAssign(
                    target=build_expr(assignment.left),
                    value=build_expr(assignment.right),
                ))
            elif kind == "ProceduralBlock":
                if str(symbol.procedureKind).endswith("Initial"):
                    if initial_is_inactive(symbol.body):
                        message = f"{name}: initial classified as empty after constant-guard evaluation"
                        self._warnings.append(message)
                        logger.warning(message)
                        continue
                    processes.append(build_process(symbol))
                    continue
                processes.append(build_process(symbol))
            elif kind == "Instance":
                child = self._parse_instance(symbol)
                prefix = f"{resolved_name(symbol)}__"
                names = {p.name: prefix + p.name for p in child.ports}
                names.update({n: prefix + n for d in child.declarations for n in d.names})
                if set(names.values()) & (local_names | {n for d in declarations for n in d.names}):
                    raise UnsupportedConstructError("hierarchy flattened identifier collision")

                def rename(value):
                    if isinstance(value, Ident):
                        return Ident(names.get(value.name, value.name))
                    if isinstance(value, EventControl):
                        return EventControl([(edge, names.get(sig, sig)) for edge, sig in value.events])
                    if isinstance(value, list):
                        return [rename(v) for v in value]
                    if isinstance(value, tuple):
                        return tuple(rename(v) for v in value)
                    if is_dataclass(value):
                        return type(value)(**{f.name: rename(getattr(value, f.name)) for f in fields(value)})
                    return value

                for port in child.ports:
                    port_symbol = next(p for p in symbol.body.portList if str(p.name) == port.name)
                    is_variable = str(port_symbol.internalSymbol.kind).endswith("Variable")
                    declarations.append(Declaration(
                        kind="reg" if is_variable and port.direction == PortDirection.Output else "wire",
                        data_type=port.data_type, names=[names[port.name]],
                        bit_width=port.bit_width,
                        signed=port.signed,
                    ))
                for declaration in child.declarations:
                    declaration.names = [names[n] for n in declaration.names]
                    declarations.append(declaration)
                child_processes = rename(child.processes)

                def assigned_roots(value):
                    """Collect real procedural drivers before hierarchy renaming."""
                    if isinstance(value, (BlockingAssign, NonBlockingAssign)):
                        target = value.target
                        while isinstance(target, (BitSelect, BitRange, ArraySelect)):
                            target = target.expr
                        if isinstance(target, Ident):
                            yield target.name
                    if isinstance(value, list):
                        for item in value:
                            yield from assigned_roots(item)
                    elif isinstance(value, tuple):
                        for item in value:
                            yield from assigned_roots(item)
                    elif is_dataclass(value):
                        for field in fields(value):
                            yield from assigned_roots(getattr(value, field.name))

                driven_child_ports = {
                    name
                    for assignment in child.continuous_assigns
                    for name in assigned_roots(BlockingAssign(assignment.target, assignment.value))
                }
                driven_child_ports.update(
                    name for process in child.processes for name in assigned_roots(process.body)
                )
                for conn in symbol.portConnections:
                    port_name = str(conn.port.name)
                    direction = str(conn.port.direction).lower()
                    expression = conn.expression
                    if direction.endswith("inout"):
                        raise UnsupportedConstructError("bidirectional hierarchy binding unsupported")
                    if expression is None:
                        if direction.endswith("out"):
                            # The child output remains represented in flattened
                            # state but has no parent-side load to drive.
                            continue
                        width = int(conn.port.type.bitWidth)
                        bound = unknown_literal(width, "'z")
                        continuous_assigns.append(ContinuousAssign(Ident(names[port_name]), bound))
                        continue
                    if direction.endswith("out"):
                        # An undriven child output is a high-impedance source in
                        # RTL, not a second driver. In particular, generated
                        # wrappers may legally drive the connected parent net.
                        if port_name not in driven_child_ports:
                            continue
                        if str(expression.kind).endswith("Assignment"):
                            expression = expression.left
                        continuous_assigns.append(ContinuousAssign(build_expr(expression), Ident(names[port_name])))
                    else:
                        bound = build_expr(expression)
                        continuous_assigns.append(ContinuousAssign(Ident(names[port_name]), bound))
                        # Clock/reset events refer to the actual parent signal,
                        # not to a combinational copy stored in the child model.
                        for process in child_processes:
                            if process.event_control:
                                events = []
                                for edge, signal in process.event_control.events:
                                    if signal == names[port_name]:
                                        if not isinstance(bound, Ident):
                                            raise UnsupportedConstructError("derived hierarchy event expression unsupported")
                                        signal = bound.name
                                    events.append((edge, signal))
                                process.event_control.events = events
                continuous_assigns.extend(rename(child.continuous_assigns))
                processes.extend(child_processes)
            elif kind in {"GenerateBlock", "GenerateBlockArray", "InstanceArray"}:
                # These scopes can contain state and instances. Reject until
                # scope-aware lowering exists; never emit a partial model.
                if kind == "GenerateBlock" and getattr(symbol, "isUninstantiated", False):
                    continue
                raise UnsupportedConstructError(f"unsupported elaborated scope: {kind}")

        return Module(
            name=name,
            parameters=[
                Parameter(
                    parameter.name,
                    IntLit(parameter.value if isinstance(parameter.value, int) else 0),
                )
                for parameter in design_info.parameters
            ] if design_info else [],
            ports=ports,
            declarations=declarations,
            continuous_assigns=continuous_assigns,
            processes=processes,
            instances=instances,
        )

    def _extract_port_types(self, syntax) -> dict[str, tuple[DataType, Optional[BitWidth]]]:
        """从语法树提取端口类型信息"""
        port_type_map = {}
        if not syntax:
            return port_type_map

        header = getattr(syntax, "header", None)
        if not header:
            return port_type_map

        port_list_node = getattr(header, "ports", None)
        if not port_list_node:
            return port_type_map

        ports_node = getattr(port_list_node, "ports", None)
        if not ports_node:
            return port_type_map

        for p in ports_node:
            if isinstance(p, pyslang.Token):
                continue
            # 获取完整声明文本
            text = str(p).strip()
            # 获取名称
            declarator = getattr(p, "declarator", None)
            if declarator and hasattr(declarator, "name"):
                name = str(getattr(declarator.name, "value", declarator.name)).strip()
                data_type, bit_width = _sv_type_to_data_type(text)
                port_type_map[name] = (data_type, bit_width)

        return port_type_map

    def _parse_port_symbol(
        self,
        port_sym,
        port_type_map: dict = None,
        semantic_port: Optional[PortInfo] = None,
    ) -> Optional[Port]:
        """从端口符号解析"""
        name = port_sym.name
        port_type_map = port_type_map or {}

        # 获取方向
        direction = PortDirection.Input
        if hasattr(port_sym, "direction"):
            direction = _pyslang_direction(port_sym.direction)

        # elaborated semantic type is authoritative for parameterized and
        # non-ANSI declarations.
        if semantic_port is not None:
            data_type, _ = _sv_type_to_data_type(semantic_port.type_text)
            bit_width = (
                BitWidth(semantic_port.width - 1, 0)
                if semantic_port.width > 1 else None
            )
        elif name in port_type_map:
            data_type, bit_width = port_type_map[name]
        else:
            data_type = DataType.Logic
            bit_width = None

        return Port(
            direction=direction,
            data_type=data_type,
            name=name,
            bit_width=bit_width,
            signed=semantic_port.signed if semantic_port else False,
        )

    def _parse_continuous_assign(self, node) -> Optional[ContinuousAssign]:
        """解析连续赋值"""
        if hasattr(node, "assignment"):
            assign = node.assignment
            left = getattr(assign, "left", None)
            right = getattr(assign, "right", None)
            if left and right:
                return ContinuousAssign(
                    target=_ExprBuilder.build(left),
                    value=_ExprBuilder.build(right),
                )
        return None

    def _parse_process(self, node) -> Optional[Process]:
        """解析进程"""
        kind = str(getattr(node, "kind", ""))

        if "Initial" in kind:
            proc_type = ProcessType.Initial
        else:
            proc_type = ProcessType.Always

        # 解析时序控制
        event_control = None
        stmt_node = getattr(node, "statement", None)
        if stmt_node:
            event_control = self._parse_timing_control(stmt_node)

        # 解析语句体
        body_stmt = NoStmt()
        if stmt_node:
            # TimingControlStatementSyntax has 'statement' for the body
            inner = getattr(stmt_node, "statement", None)
            if inner:
                body_stmt = _StmtBuilder.build(inner)

        return Process(
            proc_type=proc_type,
            event_control=event_control,
            body=body_stmt,
        )

    def _parse_timing_control(self, stmt_node) -> Optional[EventControl]:
        """解析时序控制"""
        # TimingControlStatementSyntax has 'timingControl'
        timing = getattr(stmt_node, "timingControl", None)
        if not timing:
            return None

        events = []
        kind = str(getattr(timing, "kind", ""))

        if "EventControl" in kind:
            # 尝试获取事件表达式
            expr_str = str(timing)
            for m in re.finditer(r"posedge\s+(\w+)", expr_str):
                events.append(("posedge", m.group(1)))
            for m in re.finditer(r"negedge\s+(\w+)", expr_str):
                events.append(("negedge", m.group(1)))

        if events:
            return EventControl(events=events)
        return None

    def _parse_declaration(self, node) -> list[Declaration]:
        """解析变量声明"""
        decls = []
        if hasattr(node, "declarators"):
            type_str = str(getattr(node, "type", ""))
            data_type, bit_width = _sv_type_to_data_type(type_str)

            names = []
            for decl in node.declarators:
                name_node = getattr(decl, "name", None)
                if name_node:
                    names.append(str(getattr(name_node, "value", "")))

            if names:
                kind = type_str.split("[")[0].strip().lower()
                decls.append(Declaration(
                    kind=kind,
                    data_type=data_type,
                    names=names,
                    bit_width=bit_width,
                ))
        return decls

    def _parse_instance_node(self, node) -> Optional[ModuleInstance]:
        """解析模块实例化"""
        if hasattr(node, "instances"):
            for inst in node.instances:
                name = str(getattr(inst, "name", "inst"))
                type_name = str(getattr(inst, "type", "unknown"))
                connections = []
                if hasattr(inst, "portConnections"):
                    for conn in inst.portConnections:
                        port_name = str(getattr(conn, "port", ""))
                        expr = getattr(conn, "expression", None)
                        if expr:
                            connections.append((port_name, _ExprBuilder.build(expr)))
                return ModuleInstance(
                    instance_name=name,
                    module_name=type_name,
                    port_connections=connections,
                )
        return None
