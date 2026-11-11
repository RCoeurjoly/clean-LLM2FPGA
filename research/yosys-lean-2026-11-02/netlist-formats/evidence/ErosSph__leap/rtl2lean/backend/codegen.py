"""
代码生成器 - 将 IR 转换为 Lean 4 代码
"""

from __future__ import annotations

import re

from ..middle_end.ir import (
    IRFunction,
    IRInstr,
    IRModule,
    IRType,
    IRValue,
    order_combinational_functions,
    normalize_combinational_schedule,
)


class CodeGenerationError(RuntimeError):
    """The IR cannot be represented by the current Lean backend."""


class CodeGenerator:
    """Lean 4 代码生成器"""

    def __init__(self, include_comments: bool = True):
        self.indent_level = 0
        self.code_lines: list[str] = []
        self.include_comments = include_comments
        self._pending_helpers: list[str] = []
        self._helper_serial = 0
        self._helper_names: set[str] = set()
        self._has_comb = False

    @classmethod
    def generate(cls, ir_mod: IRModule, include_comments: bool = True, emit_bitvec: bool = False,
                 already_normalized: bool = False) -> str:
        """生成 Lean 4 代码"""
        gen = cls(include_comments)
        normalized = ir_mod if already_normalized else normalize_combinational_schedule(ir_mod)
        gen._generate_module(normalized)
        return "\n".join(gen.code_lines)

    # ───────────────────────── 工具方法 ─────────────────────────

    def _indent(self) -> str:
        return "  " * self.indent_level

    def _add(self, line: str = "") -> None:
        self.code_lines.append(f"{self._indent()}{line}")

    def _blank(self) -> None:
        self.code_lines.append("")

    def _comment(self, text: str) -> None:
        if self.include_comments:
            self._add(f"-- {text}")

    def _doc(self, text: str) -> None:
        if self.include_comments:
            self._add(f"/-- {text} -/")

    @staticmethod
    def ir_type_to_lean(typ: IRType) -> str:
        """将 IR 类型转换为 Lean 类型"""
        if typ.name == "void":
            return "Unit"
        if typ.name == "bool":
            return "Bool"
        if typ.name in ("clock", "reset"):
            return "Bool"
        if typ.name == "int":
            return f"BitVec {typ.width}"
        if typ.name in ("uint", "reg", "wire"):
            if typ.width <= 1:
                return "Bool"
            return f"BitVec {typ.width}"
        return f"BitVec {typ.width}"

    @staticmethod
    def ir_value_width(value: IRValue) -> int:
        """Return the SystemVerilog bit width carried by an IR expression."""
        if value.kind == "lean_local":
            return value.data["width"]
        if value.kind == "const_bool":
            return 1
        if value.kind in ("const_int", "const_nat"):
            return int(value.data.get("width", 32))
        if value.kind == "var":
            return int(value.data["type"].width)
        if value.kind == "unary":
            if value.data["op"] in {"!", "&", "|", "^", "reduce_xor", "~&", "~|", "~^"}:
                return 1
            return CodeGenerator.ir_value_width(value.data["operand"])
        if value.kind == "binary":
            if value.data["op"] in {"<<", ">>", "<<<", ">>>"}:
                return CodeGenerator.ir_value_width(value.data["left"])
            if value.data["op"] in {"==", "!=", "<", ">", "<=", ">=", "&&", "||"}:
                return 1
            return max(
                CodeGenerator.ir_value_width(value.data["left"]),
                CodeGenerator.ir_value_width(value.data["right"]),
            )
        if value.kind == "bit_select":
            return 1
        if value.kind == "array_select":
            return int(value.data["element_width"])
        if value.kind == "array_store":
            return CodeGenerator.ir_value_width(value.data["memory"])
        if value.kind in {"bit_store", "range_store"}:
            return CodeGenerator.ir_value_width(value.data["base"])
        if value.kind == "bit_range":
            return int(value.data["hi"]) - int(value.data["lo"]) + 1
        if value.kind == "concat":
            return sum(CodeGenerator.ir_value_width(item) for item in value.data["values"])
        if value.kind == "phi":
            return max(
                CodeGenerator.ir_value_width(value.data["then"]),
                CodeGenerator.ir_value_width(value.data["else"]),
            )
        raise CodeGenerationError(f"cannot determine width of IR value kind {value.kind!r}")

    @staticmethod
    def _is_bool_value(value: IRValue) -> bool:
        """Actual Lean representation, not simply the hardware bit width."""
        if CodeGenerator.ir_value_width(value) != 1:
            return False
        if value.kind == "lean_local":
            return value.data.get("representation", "Bool") == "Bool"
        if value.kind in {"bit_range", "concat", "array_select", "array_store"}:
            return False
        if value.kind == "unary" and value.data["op"] == "+":
            return CodeGenerator._is_bool_value(value.data["operand"])
        return True

    @staticmethod
    def _as_bool(value: IRValue, ir_mod: IRModule | None) -> str:
        rendered = CodeGenerator.ir_value_to_lean(value, ir_mod)
        if CodeGenerator._is_bool_value(value):
            return rendered
        if CodeGenerator.ir_value_width(value) == 1:
            return f"bitVecToBool ({rendered})"
        return f"bvNonzero ({rendered})"

    @staticmethod
    def _as_bitvec(value: IRValue, ir_mod: IRModule | None) -> str:
        rendered = CodeGenerator.ir_value_to_lean(value, ir_mod)
        if CodeGenerator._is_bool_value(value):
            return f"boolToBitVec ({rendered})"
        return rendered

    @staticmethod
    def _as_bitvec_width(value: IRValue, width: int, ir_mod: IRModule | None) -> str:
        rendered = CodeGenerator._as_bitvec(value, ir_mod)
        if CodeGenerator.ir_value_width(value) != width:
            return f"BitVec.setWidth {width} ({rendered})"
        return rendered

    @staticmethod
    def _as_shift_amount(value: IRValue, ir_mod: IRModule | None) -> str:
        rendered = CodeGenerator.ir_value_to_lean(value, ir_mod)
        if CodeGenerator._is_bool_value(value):
            return f"boolToNat ({rendered})"
        return f"({rendered}).toNat"

    @staticmethod
    def _as_nat(value: IRValue, ir_mod: IRModule | None) -> str:
        rendered = CodeGenerator.ir_value_to_lean(value, ir_mod)
        if CodeGenerator._is_bool_value(value):
            return f"boolToNat ({rendered})"
        return f"({rendered}).toNat"

    @staticmethod
    def ir_value_to_lean(value: IRValue, ir_mod: IRModule | None = None) -> str:
        """Render an IR value while preserving its hardware width and Bool/BitVec kind."""
        if value.kind == "lean_local":
            return value.data["name"]
        if value.kind == "const_bool":
            return str(value.data["value"]).lower()
        if value.kind in ("const_int", "const_nat"):
            width = int(value.data.get("width", 32))
            raw = int(value.data["value"])
            if width <= 1:
                return "true" if raw & 1 else "false"
            # Constant folding can represent a bitwise complement as a
            # negative Python integer. ofNat requires a natural literal.
            if raw < 0:
                raw %= 1 << width
            return f"BitVec.ofNat {width} {raw}"
        if value.kind == "var":
            name = value.data["name"]
            if ir_mod is None:
                return name
            input_names = {port for port, _, is_input in ir_mod.ports if is_input}
            return f"i.{name}" if name in input_names else f"s.{name}"
        if value.kind == "unary":
            op = value.data["op"]
            operand_value = value.data["operand"]
            operand = CodeGenerator.ir_value_to_lean(operand_value, ir_mod)
            width = CodeGenerator.ir_value_width(operand_value)
            if op == "!":
                return f"!({CodeGenerator._as_bool(operand_value, ir_mod)})"
            if op == "~":
                return f"!({CodeGenerator._as_bool(operand_value, ir_mod)})" if width == 1 else f"~~~({operand})"
            if op in {"&", "|", "^", "reduce_xor", "~&", "~|", "~^"}:
                base_op = op[1:] if op.startswith("~") else op
                helper = {"&": "bvReduceAnd", "|": "bvNonzero", "^": "bvReduceXor", "reduce_xor": "bvReduceXor"}[base_op]
                reduced = CodeGenerator._as_bool(operand_value, ir_mod) if width == 1 else f"{helper} ({operand})"
                return f"!({reduced})" if op.startswith("~") else reduced
            if op == "+":
                return operand
            if op == "-":
                if width == 1:
                    return f"bitVecToBool (-({CodeGenerator._as_bitvec(operand_value, ir_mod)}))"
                return f"-({operand})"
            raise CodeGenerationError(f"unsupported IR unary operator: {op}")
        if value.kind == "binary":
            left_value = value.data["left"]
            right_value = value.data["right"]
            left = CodeGenerator.ir_value_to_lean(left_value, ir_mod)
            right = CodeGenerator.ir_value_to_lean(right_value, ir_mod)
            op = value.data["op"]
            result_width = CodeGenerator.ir_value_width(value)
            if op in {"&&", "||"}:
                lean_op = "&&" if op == "&&" else "||"
                return f"({CodeGenerator._as_bool(left_value, ir_mod)} {lean_op} {CodeGenerator._as_bool(right_value, ir_mod)})"
            if op in {"==", "!=", "<", ">", "<=", ">="}:
                # SystemVerilog first extends comparison operands to a common
                # width. Comparing their unsigned Nat interpretations has the
                # same value semantics and also handles differently-sized
                # unsized literals without asking Lean to unify BitVec widths.
                left_nat = CodeGenerator._as_nat(left_value, ir_mod)
                right_nat = CodeGenerator._as_nat(right_value, ir_mod)
                relation = "=" if op == "==" else "≠" if op == "!=" else op
                return f"decide ({left_nat} {relation} {right_nat})"
            if op in {"&", "|", "^", "~^"}:
                if result_width == 1:
                    bool_left = CodeGenerator._as_bool(left_value, ir_mod)
                    bool_right = CodeGenerator._as_bool(right_value, ir_mod)
                    if op == "&":
                        return f"({bool_left} && {bool_right})"
                    if op == "|":
                        return f"({bool_left} || {bool_right})"
                    xor = f"Bool.xor ({bool_left}) ({bool_right})"
                    return f"!({xor})" if op == "~^" else xor
                lean_op = {"&": "&&&", "|": "|||", "^": "^^^", "~^": "^^^"}[op]
                left = CodeGenerator._as_bitvec_width(left_value, result_width, ir_mod)
                right = CodeGenerator._as_bitvec_width(right_value, result_width, ir_mod)
                body = f"({left} {lean_op} {right})"
                return f"~~~{body}" if op == "~^" else body
            if op in {"<<", ">>", "<<<", ">>>"}:
                amount = CodeGenerator._as_shift_amount(right_value, ir_mod)
                if op in {"<<", "<<<"}:
                    body = f"BitVec.shiftLeft ({CodeGenerator._as_bitvec(left_value, ir_mod)}) {amount}"
                elif op == ">>":
                    body = f"BitVec.ushiftRight ({CodeGenerator._as_bitvec(left_value, ir_mod)}) {amount}"
                else:
                    body = f"BitVec.sshiftRight ({CodeGenerator._as_bitvec(left_value, ir_mod)}) {amount}"
                return f"bitVecToBool ({body})" if result_width == 1 else body
            if op in {"+", "-", "*", "/", "%"}:
                lean_op = {"+": "+", "-": "-", "*": "*", "/": "/", "%": "%"}[op]
                if result_width == 1:
                    body = f"({CodeGenerator._as_bitvec(left_value, ir_mod)} {lean_op} {CodeGenerator._as_bitvec(right_value, ir_mod)})"
                    return f"bitVecToBool {body}"
                left = CodeGenerator._as_bitvec_width(left_value, result_width, ir_mod)
                right = CodeGenerator._as_bitvec_width(right_value, result_width, ir_mod)
                return f"({left} {lean_op} {right})"
            raise CodeGenerationError(f"unsupported IR binary operator: {op}")
        if value.kind == "bit_select":
            val = CodeGenerator.ir_value_to_lean(value.data["value"], ir_mod)
            index = value.data["index"]
            if index.const_value is not None:
                idx = str(index.const_value)
            else:
                idx = CodeGenerator._as_shift_amount(index, ir_mod)
            if CodeGenerator.ir_value_width(value.data["value"]) == 1:
                return CodeGenerator._as_bool(value.data["value"], ir_mod)
            return f"BitVec.getLsbD ({val}) {idx}"
        if value.kind == "bit_range":
            val = CodeGenerator._as_bitvec(value.data["value"], ir_mod)
            hi = value.data["hi"]
            lo = value.data["lo"]
            return f"BitVec.extractLsb {hi} {lo} ({val})"
        if value.kind == "array_select":
            memory = CodeGenerator.ir_value_to_lean(value.data["value"], ir_mod)
            index = value.data["index"]
            index_nat = CodeGenerator._as_shift_amount(index, ir_mod)
            return (
                f"bvArrayRead {value.data['element_width']} {value.data['depth']} "
                f"{value.data['left']} {value.data['right']} ({memory}) ({index_nat})"
            )
        if value.kind == "array_store":
            memory = CodeGenerator.ir_value_to_lean(value.data["memory"], ir_mod)
            index_nat = CodeGenerator._as_shift_amount(value.data["index"], ir_mod)
            stored = CodeGenerator._as_bitvec(value.data["value"], ir_mod)
            return (
                f"bvArrayWrite {value.data['element_width']} {value.data['depth']} "
                f"{value.data['left']} {value.data['right']} ({memory}) "
                f"({index_nat}) ({stored})"
            )
        if value.kind == "bit_store":
            base_value = value.data["base"]
            width = CodeGenerator.ir_value_width(base_value)
            base = CodeGenerator._as_bitvec(base_value, ir_mod)
            index = CodeGenerator._as_shift_amount(value.data["index"], ir_mod)
            stored = CodeGenerator._as_bool(value.data["value"], ir_mod)
            rendered = f"bvBitWrite {width} ({base}) ({index}) ({stored})"
            return f"bitVecToBool ({rendered})" if width == 1 else rendered
        if value.kind == "range_store":
            base_value = value.data["base"]
            width = CodeGenerator.ir_value_width(base_value)
            stored = CodeGenerator._as_bitvec(value.data["value"], ir_mod)
            rendered = (
                f"bvRangeWrite {width} {value.data['hi']} {value.data['lo']} "
                f"({CodeGenerator._as_bitvec(base_value, ir_mod)}) ({stored})"
            )
            return f"bitVecToBool ({rendered})" if width == 1 else rendered
        if value.kind == "concat":
            parts = []
            widths = []
            for item in value.data["values"]:
                parts.append(CodeGenerator._as_bitvec(item, ir_mod))
                widths.append(CodeGenerator.ir_value_width(item))
            if not parts:
                raise CodeGenerationError("empty concatenation")
            def balanced_append(lo: int, hi: int) -> tuple[str, int]:
                if hi - lo == 1:
                    return parts[lo], widths[lo]
                mid = (lo + hi) // 2
                left, left_width = balanced_append(lo, mid)
                right, right_width = balanced_append(mid, hi)
                return (f"BitVec.append (n := {left_width}) (m := {right_width}) "
                        f"({left}) ({right})", left_width + right_width)
            # Concatenation is associative with order preserved. A balanced
            # tree keeps Lean syntax/elaboration depth logarithmic for ROMs.
            return balanced_append(0, len(parts))[0]
        if value.kind == "call":
            args = ", ".join(CodeGenerator.ir_value_to_lean(a, ir_mod) for a in value.data["args"])
            return f"{value.data['name']} ({args})"
        if value.kind == "phi":
            cond = CodeGenerator._as_bool(value.data["cond"], ir_mod)
            width = CodeGenerator.ir_value_width(value)
            render = CodeGenerator._as_bool if width == 1 else lambda arm,mod: CodeGenerator._as_bitvec_width(arm,width,mod)
            then_value = render(value.data["then"], ir_mod)
            else_value = render(value.data["else"], ir_mod)
            return f"(if {cond} then {then_value} else {else_value})"
        raise CodeGenerationError(f"unsupported IR value kind: {value.kind}")

    # ───────────────────────── 代码生成 ─────────────────────────

    def _generate_module(self, ir_mod: IRModule) -> None:
        """生成模块的 Lean 代码"""
        self._add("/-")
        self._add(f"自动生成的 Lean 4 代码")
        self._add(f"源模块：{ir_mod.name}")
        self._add(f"生成时间：Lean 4 RTL 编译器")
        self._add("-/")
        self._blank()

        # 导入
        self._add("import Std")
        self._add("set_option linter.unusedVariables false")
        self._add("set_option maxRecDepth 100000")
        self._add("set_option maxHeartbeats 8000000")
        self._blank()
        self._add(f"namespace {ir_mod.name}")
        self._blank()

        # 类型定义
        self._gen_type_defs(ir_mod)

        # 状态结构
        self._gen_state_struct(ir_mod)

        # 输入结构
        self._gen_input_struct(ir_mod)

        # 输出结构
        self._gen_output_struct(ir_mod)

        # 初始状态
        self._gen_init_state(ir_mod)

        # 默认输入
        self._gen_default_inputs(ir_mod)

        # 组合逻辑函数
        self._gen_combinational_funcs(ir_mod)

        # 时序逻辑函数（包含 step 生成）
        self._gen_sequential_funcs(ir_mod)

        # 输出助手
        self._gen_output_function(ir_mod)

        # 不变量
        self._gen_invariants(ir_mod)

        # 定理骨架
        self._gen_theorem_skeleton(ir_mod)

        self._blank()
        self._add(f"end {ir_mod.name}")

    def _gen_state_struct(self, ir_mod: IRModule) -> None:
        """生成状态结构"""
        fields = [(name, "Bool" if width <= 1 else f"BitVec {width}")
                  for name, width in ir_mod.registers]
        fields.extend((name, self.ir_type_to_lean(typ)) for name, typ in ir_mod.signals)
        # Keep concrete constructors bounded. Lean inheritance preserves public
        # projections and record-update syntax; every original field remains.
        # This is a field-count allocation policy, not a BitVec width rule.
        chunk_size = 64
        if len(fields) > chunk_size:
            parents = []
            serial = 0

            def fresh_name():
                nonlocal serial
                while True:
                    name = f"{ir_mod.name}StateBlock{serial}"
                    serial += 1
                    if all(field != f"to{name}" for field, _ in fields):
                        return name

            for start in range(0, len(fields), chunk_size):
                name = fresh_name()
                self._add(f"structure {name} where")
                self.indent_level += 1
                for field, typ in fields[start:start + chunk_size]:
                    self._add(f"{field} : {typ}")
                self.indent_level -= 1
                self._blank()
                parents.append(name)
            while len(parents) > chunk_size:
                next_parents = []
                for start in range(0, len(parents), chunk_size):
                    name = fresh_name()
                    self._add(f"structure {name} extends " +
                              ", ".join(parents[start:start + chunk_size]) + " where")
                    self._blank()
                    next_parents.append(name)
                parents = next_parents
            self._doc(f"{ir_mod.name} 状态结构（保留全部字段的嵌套具体记录）")
            self._add(f"structure {ir_mod.name}State extends " + ", ".join(parents) + " where")
            self._blank()
            return
        self._doc(f"{ir_mod.name} 状态结构")
        self._add(f"structure {ir_mod.name}State where")
        self.indent_level += 1

        for name, width in ir_mod.registers:
            if width <= 1:
                lean_type = "Bool"
            else:
                lean_type = f"BitVec {width}"
            self._add(f"{name} : {lean_type}")

        for name, typ in ir_mod.signals:
            lean_type = self.ir_type_to_lean(typ)
            self._add(f"{name} : {lean_type}")

        self.indent_level -= 1
        self._blank()

    def _gen_type_defs(self, ir_mod: IRModule) -> None:
        """生成类型定义"""
        self._comment("类型定义与 SystemVerilog 位向量辅助函数")
        self._add("def boolToNat (value : Bool) : Nat := if value then 1 else 0")
        self._add("def boolToBitVec (value : Bool) : BitVec 1 := BitVec.ofNat 1 (boolToNat value)")
        self._add("def bitVecToBool (value : BitVec 1) : Bool := BitVec.getLsbD value 0")
        self._add("def bvNonzero {width : Nat} (value : BitVec width) : Bool := value != 0")
        self._add("def bvReduceAnd {width : Nat} (value : BitVec width) : Bool := value == ~~~(0 : BitVec width)")
        self._add("def bvReduceXor {width : Nat} (value : BitVec width) : Bool :=")
        self.indent_level += 1
        self._add("(List.range width).foldl (fun acc index => Bool.xor acc (BitVec.getLsbD value index)) false")
        self.indent_level -= 1
        self._add("def arrayOffset (left right index : Nat) : Nat :=")
        self.indent_level += 1
        self._add("if left <= right then index - left else left - index")
        self.indent_level -= 1
        self._add("def bvArrayRead (elementWidth depth left right : Nat)")
        self.indent_level += 1
        self._add("(memory : BitVec (elementWidth * depth)) (index : Nat) : BitVec elementWidth :=")
        self.indent_level += 1
        self._add("BitVec.extractLsb' (arrayOffset left right index * elementWidth) elementWidth memory")
        self.indent_level -= 2
        self._add("def bvArrayWrite (elementWidth depth left right : Nat)")
        self.indent_level += 1
        self._add("(memory : BitVec (elementWidth * depth)) (index : Nat)")
        self._add("(value : BitVec elementWidth) : BitVec (elementWidth * depth) :=")
        self.indent_level += 1
        self._add("let shift := arrayOffset left right index * elementWidth")
        self._add("let wideValue := BitVec.setWidth (elementWidth * depth) value")
        self._add("let elementMask : BitVec elementWidth := ~~~(0 : BitVec elementWidth)")
        self._add("let wideMask := BitVec.setWidth (elementWidth * depth) elementMask")
        self._add("(memory &&& ~~~(BitVec.shiftLeft wideMask shift)) |||")
        self.indent_level += 1
        self._add("BitVec.shiftLeft wideValue shift")
        self.indent_level -= 3
        self._add("def bvBitWrite (width : Nat) (base : BitVec width) (index : Nat)")
        self.indent_level += 1
        self._add("(value : Bool) : BitVec width :=")
        self.indent_level += 1
        self._add("let mask := BitVec.shiftLeft (BitVec.ofNat width 1) index")
        self._add("if value then base ||| mask else base &&& ~~~mask")
        self.indent_level -= 2
        self._add("def bvRangeWrite (width hi lo : Nat) (base : BitVec width)")
        self.indent_level += 1
        self._add("(value : BitVec (hi - lo + 1)) : BitVec width :=")
        self.indent_level += 1
        self._add("let wideValue := BitVec.setWidth width value")
        self._add("let sliceMask : BitVec (hi - lo + 1) := ~~~(0 : BitVec (hi - lo + 1))")
        self._add("let wideMask := BitVec.setWidth width sliceMask")
        self._add("(base &&& ~~~(BitVec.shiftLeft wideMask lo)) |||")
        self.indent_level += 1
        self._add("BitVec.shiftLeft wideValue lo")
        self.indent_level -= 3
        self._blank()

    def _gen_input_struct(self, ir_mod: IRModule) -> None:
        """生成输入结构"""
        input_ports = [(name, typ) for name, typ, is_in in ir_mod.ports if is_in]
        if not input_ports:
            return

        self._doc(f"{ir_mod.name} 输入信号")
        self._add(f"structure {ir_mod.name}Inputs where")
        self.indent_level += 1

        for name, typ in input_ports:
            if name.lower() in ("clk", "rst", "rst_n", "reset", "reset_n"):
                lean_type = "Bool"
            else:
                lean_type = self.ir_type_to_lean(typ)
            self._add(f"{name} : {lean_type}")

        self.indent_level -= 1
        self._blank()

    def _gen_output_struct(self, ir_mod: IRModule) -> None:
        """生成输出结构 — 类型从 State 对应字段继承"""
        output_ports = [(name, typ) for name, typ, is_in in ir_mod.ports if not is_in]
        if not output_ports:
            return

        # 建立字段名到 State 类型的映射
        field_types = {}
        for name, width in ir_mod.registers:
            field_types[name] = "Bool" if width <= 1 else f"BitVec {width}"
        for name, typ in ir_mod.signals:
            w = getattr(typ, 'width', 1) or 1
            field_types[name] = "Bool" if w <= 1 else f"BitVec {w}"

        self._doc(f"{ir_mod.name} 输出信号")
        self._add(f"structure {ir_mod.name}Outputs where")
        self.indent_level += 1

        for name, typ in output_ports:
            lean_type = field_types.get(name, self.ir_type_to_lean(typ))
            self._add(f"{name} : {lean_type}")

        self.indent_level -= 1
        self._blank()

    def _gen_default_inputs(self, ir_mod: IRModule) -> None:
        """生成默认输入信号"""
        input_ports = [(name, typ) for name, typ, is_in in ir_mod.ports if is_in]
        if not input_ports:
            return

        self._doc(f"{ir_mod.name} 默认输入值")
        self._add(f"def defaultInputs : {ir_mod.name}Inputs where")
        self.indent_level += 1

        for name, typ in input_ports:
            lean_type = self.ir_type_to_lean(typ)
            if "clk" in name.lower():
                self._add(f"{name} := false")
            elif "rst" in name.lower() or "reset" in name.lower():
                self._add(f"{name} := true")
            elif lean_type == "Bool":
                self._add(f"{name} := false")
            elif lean_type.startswith("BitVec"):
                w = getattr(typ, 'width', 1) or 1
                self._add(f"{name} := BitVec.ofNat {w} 0")
            else:
                self._add(f"{name} := 0")

        self.indent_level -= 1
        self._blank()

    def _gen_init_state(self, ir_mod: IRModule) -> None:
        """生成初始状态定义"""
        self._doc(f"{ir_mod.name} 初始状态")
        if not ir_mod.registers and not ir_mod.signals:
            self._add(f"def init : {ir_mod.name}State := {{}}")
        else:
            self._add(f"def init : {ir_mod.name}State where")
            self.indent_level += 1

            for name, width in ir_mod.registers:
                initial = ir_mod.initial_values.get(name)
                if initial is not None:
                    self._add(f"{name} := {self.ir_value_to_lean(initial, ir_mod)}")
                    continue
                if width <= 1:
                    self._add(f"{name} := false")
                else:
                    self._add(f"{name} := BitVec.ofNat {width} 0")

            for name, typ in ir_mod.signals:
                initial = ir_mod.initial_values.get(name)
                if initial is not None:
                    self._add(f"{name} := {self.ir_value_to_lean(initial, ir_mod)}")
                    continue
                w = getattr(typ, 'width', 1) or 1
                if w <= 1:
                    self._add(f"{name} := false")
                else:
                    self._add(f"{name} := BitVec.ofNat {w} 0")

            self.indent_level -= 1
        self._blank()

    def _gen_combinational_funcs(self, ir_mod: IRModule) -> None:
        """生成组合逻辑函数"""
        for func in ir_mod.functions:
            if not func.is_sequential:
                self._gen_comb_func(func, ir_mod)
        ordered = order_combinational_functions([
            function for function in ir_mod.functions if not function.is_sequential
        ])
        if not ordered:
            self._has_comb = False
            return
        schedule = self._gen_bounded_comb_helpers(ir_mod, ordered)
        self._doc("Combinational fixed-point schedule derived from IR dependencies")
        self._add(f"def comb (s : {ir_mod.name}State) (i : {ir_mod.name}Inputs) : {ir_mod.name}State :=")
        self.indent_level += 1
        self._gen_comb_chain(schedule, "s", "result")
        self._add("result")
        self.indent_level -= 1
        self._blank()
        self._has_comb = True

    def _render_shared_value(self, value: IRValue, ir_mod: IRModule) -> str:
        """Render large pure expression trees as a typed, shared let-bound DAG.

        Keys include every operation, type and scalar attribute; no arithmetic
        rewriting is performed. All references still read the same old State.
        The local references exist only during rendering, never in module IR.
        """
        pending = [value]
        count = 0
        # Very large trees are represented as a typed DAG.  Keeping medium
        # expressions direct is materially faster for Lean's elaborator.
        sharing_threshold = 512
        while pending and count <= sharing_threshold:
            item = pending.pop()
            count += 1
            for child in item.data.values():
                if isinstance(child, IRValue):
                    pending.append(child)
                elif isinstance(child, list):
                    pending.extend(x for x in child if isinstance(x, IRValue))
        if count <= sharing_threshold:
            return self.ir_value_to_lean(value, ir_mod)

        interned = {}
        identity_cache = {}
        bindings = []
        definitions = []
        pure = {"const_bool", "const_int", "const_nat", "var", "unary", "binary",
                "bit_select", "bit_range", "array_select", "array_store",
                "bit_store", "range_store", "concat", "phi"}

        def transform(data):
            if isinstance(data, IRValue):
                key_id, ref = intern(data)
                return ("expr", key_id), ref
            if isinstance(data, IRType):
                return ("type", data.name, data.width), data
            if isinstance(data, list):
                pairs = [transform(x) for x in data]
                return ("list", tuple(k for k, _ in pairs)), [v for _, v in pairs]
            return (type(data).__name__, data), data

        def intern(node):
            cached = identity_cache.get(id(node))
            if cached is not None:
                return cached
            if node.kind not in pure:
                raise CodeGenerationError(f"expression sharing requires pure IR, got {node.kind}")
            pairs = {k: transform(v) for k, v in node.data.items()}
            key = (node.kind, tuple(sorted((k, p[0]) for k, p in pairs.items())))
            if key not in interned:
                shallow = IRValue(node.kind, {k: p[1] for k, p in pairs.items()})
                if node.kind in {"const_bool", "const_int", "const_nat", "var"}:
                    ref = shallow
                else:
                    width = self.ir_value_width(shallow)
                    name = f"_rtl_expr_{len(bindings)}"
                    typ = "Bool" if self._is_bool_value(shallow) else f"BitVec {width}"
                    bindings.append(f"let {name} : {typ} := {self.ir_value_to_lean(shallow, ir_mod)}")
                    definitions.append((name, width, shallow))
                    ref = IRValue("lean_local", dict(name=name, width=width, representation=typ))
                interned[key] = (len(interned), ref)
            result = interned[key]
            identity_cache[id(node)] = result
            return result

        _, root = intern(value)
        if len(definitions) > 128:
            return self._render_let_chunks(definitions, root, ir_mod)
        indent = "  " * (self.indent_level + 2)
        return "(\n" + "\n".join(indent + line for line in bindings) + "\n" + indent + self.ir_value_to_lean(root, ir_mod) + ")"

    def _render_let_chunks(self, definitions, root, ir_mod):
        """Bound elaboration nesting while preserving the concrete expression DAG."""
        indexes = {name: j for j, (name, _, _) in enumerate(definitions)}
        last_use = [-1] * len(definitions)

        def dependencies(value):
            if value.kind == "lean_local":
                return {indexes[value.data["name"]]}
            result = set()
            for child in value.data.values():
                if isinstance(child, IRValue):
                    result.update(dependencies(child))
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, IRValue):
                            result.update(dependencies(item))
            return result

        for j, (_, _, value) in enumerate(definitions):
            for dep in dependencies(value):
                last_use[dep] = max(last_use[dep], j)
        for dep in dependencies(root):
            last_use[dep] = len(definitions)

        def tuple_expr(parts, is_type=False):
            if not parts:
                return "Unit" if is_type else "()"
            if len(parts) == 1:
                return parts[0]
            mid = len(parts) // 2
            left, right = tuple_expr(parts[:mid], is_type), tuple_expr(parts[mid:], is_type)
            return f"({left} × {right})" if is_type else f"({left}, {right})"

        def typ(j):
            width = definitions[j][1]
            return "Bool" if self._is_bool_value(definitions[j][2]) else f"BitVec {width}"

        def projections(ids, base, mapping):
            if len(ids) == 1:
                mapping[definitions[ids[0]][0]] = base
            elif ids:
                mid = len(ids) // 2
                projections(ids[:mid], f"({base}).1", mapping)
                projections(ids[mid:], f"({base}).2", mapping)

        def substitute(value, mapping):
            if value.kind == "lean_local":
                return IRValue("lean_local", dict(name=mapping[value.data["name"]],
                                                   width=value.data["width"],
                                                   representation=value.data.get("representation", "Bool")))
            data = {}
            for key, child in value.data.items():
                if isinstance(child, IRValue):
                    data[key] = substitute(child, mapping)
                elif isinstance(child, list):
                    data[key] = [substitute(x, mapping) if isinstance(x, IRValue) else x for x in child]
                else:
                    data[key] = child
            return IRValue(value.kind, data)

        self._helper_names.update(f.name for f in ir_mod.functions)
        expression = "()"
        for start in range(0, len(definitions), 64):
            end = min(start + 64, len(definitions))
            incoming = [j for j in range(start) if last_use[j] >= start]
            outgoing = [j for j in range(end) if last_use[j] >= end]
            name = f"_rtl_eval_block_{self._helper_serial}"
            while name in self._helper_names:
                self._helper_serial += 1
                name = f"_rtl_eval_block_{self._helper_serial}"
            self._helper_serial += 1
            self._helper_names.add(name)
            input_type = tuple_expr([typ(j) for j in incoming], True)
            output_type = tuple_expr([typ(j) for j in outgoing], True)
            lines = [f"private def {name} (s : {ir_mod.name}State) (i : {ir_mod.name}Inputs) "
                     f"(env : {input_type}) : {output_type} :="]
            mapping = {}
            projections(incoming, "env", mapping)
            for j in range(start, end):
                local_name, _, value = definitions[j]
                rendered = self.ir_value_to_lean(substitute(value, mapping), ir_mod)
                lines.append(f"  let {local_name} : {typ(j)} := {rendered}")
                mapping[local_name] = local_name
            lines.append("  " + tuple_expr([mapping[definitions[j][0]] for j in outgoing]))
            self._pending_helpers.extend(lines + [""])
            expression = f"{name} s i ({expression})"
        return expression

    def _render_state_value(self, value: IRValue, ir_mod: IRModule) -> str:
        rendered = self._render_shared_value(value, ir_mod)
        # Existing scalar State fields use Bool; vector expressions remain
        # vectors internally and convert only on entry to that typed field.
        if self.ir_value_width(value) == 1 and not self._is_bool_value(value):
            return f"bitVecToBool ({rendered})"
        return rendered

    def _gen_comb_func(self, func: IRFunction, ir_mod: IRModule) -> None:
        """生成单个组合逻辑函数 — 统一的 (s: State) (i: Inputs) 签名"""
        helper_position = len(self.code_lines)
        mod_name = ir_mod.name
        self._doc(f"组合逻辑：{func.name}")
        self._add(f"def {func.name} (s : {mod_name}State) (i : {mod_name}Inputs) : {mod_name}State :=")
        self.indent_level += 1

        # 收集赋值
        assigns = {}
        for block in func.blocks:
            if block.name == func.entry_block:
                for instr in block.instrs:
                    if instr.kind in ("assign", "nba") and instr.dest and instr.value:
                        assigns[instr.dest] = self._render_state_value(instr.value, ir_mod)

        self.code_lines[helper_position:helper_position] = self._pending_helpers
        self._pending_helpers = []
        if assigns:
            for dest, value in assigns.items():
                self._add(f"let {dest} := {value}")
            self._add("{ s with")
            self.indent_level += 1
            for dest in assigns:
                self._add(f"{dest} := {dest}")
            self.indent_level -= 1
            self._add("}")
        else:
            self._add("s")

        self.indent_level -= 1
        self._blank()

    def _gen_sequential_funcs(self, ir_mod: IRModule) -> None:
        """生成时序逻辑函数和统一的 step 函数"""
        sequential_funcs = [f for f in ir_mod.functions if f.is_sequential]
        for func in sequential_funcs:
            self._gen_seq_func(func, ir_mod)
        clocks = sorted({func.clock_signal for func in sequential_funcs if func.clock_signal})
        if len(clocks) <= 1:
            # Every model has one generic evaluation entry point, including
            # pure combinational designs.
            self._gen_commit_function(ir_mod, sequential_funcs)
            self._gen_step_function(ir_mod, sequential_funcs)
        else:
            self._gen_clock_event_semantics(ir_mod, sequential_funcs, clocks)

    def _gen_seq_func(self, func: IRFunction, ir_mod: IRModule) -> None:
        """生成单个时序逻辑函数（复位由 step 顶层统一处理）"""
        helper_position = len(self.code_lines)
        clk = func.clock_signal or "clk"
        rst = func.reset_signal or "rst"

        self._doc(f"时序逻辑: {func.name} (clk={clk}, rst={rst}) — 复位由 step 顶层处理")
        self._add(f"def {func.name} (s : {ir_mod.name}State) (i : {ir_mod.name}Inputs) : {ir_mod.name}State :=")
        self.indent_level += 1

        nba_assigns = []
        for block in func.blocks:
            if block.name == func.entry_block:
                for instr in block.instrs:
                    if instr.kind in ("assign", "nba") and instr.dest and instr.value:
                        nba_assigns.append((instr.dest, self._render_state_value(instr.value, ir_mod)))

        self.code_lines[helper_position:helper_position] = self._pending_helpers
        self._pending_helpers = []
        if nba_assigns:
            self._add("{ s with")
            self.indent_level += 1
            for dest, value in nba_assigns:
                self._add(f"{dest} := {value}")
            self.indent_level -= 1
            self._add("}")
        else:
            self._add("s")

        self.indent_level -= 1
        self._blank()

    def _gen_step_function(self, ir_mod: IRModule, sequential_funcs: list) -> None:
        """Generate pre-comb, parallel register commit, then settled outputs."""
        mod_name = ir_mod.name
        self._doc("step: pre-comb → parallel next-state commit → post-comb settle")
        self._add(f"def step (s : {mod_name}State) (i : {mod_name}Inputs) : {mod_name}State :=")
        self.indent_level += 1

        if not sequential_funcs and not self._has_comb:
            self._add("s")
        else:
            pre_state = "s"
            if self._has_comb:
                self._add("let s_pre := comb s i")
                pre_state = "s_pre"

            self._add(f"let s_next := commit {pre_state} i")

            if self._has_comb and sequential_funcs:
                self._add("let s_settled := comb s_next i")
                self._add("s_settled")
            else:
                self._add("s_next")
        self.indent_level -= 1
        self._blank()

    def _gen_commit_function(self, ir_mod: IRModule, sequential_funcs: list[IRFunction]) -> None:
        """Parallel commit for all sequential processes in a single domain."""
        self._doc("Parallel nonblocking commit for the single clock domain")
        self._add(f"def commit (s : {ir_mod.name}State) (i : {ir_mod.name}Inputs) : {ir_mod.name}State :=")
        self.indent_level += 1
        if len(sequential_funcs) == 1:
            self._add(f"{sequential_funcs[0].name} s i")
        elif len(sequential_funcs) > 1:
            self._gen_register_merge(ir_mod, sequential_funcs, "s", "result")
            self._add("result")
        else:
            self._add("s")
        self.indent_level -= 1
        self._blank()

    @staticmethod
    def _clock_constructor(clock: str, used: set[str]) -> str:
        base = re.sub(r"[^A-Za-z0-9_]", "_", clock)
        if not base or not base[0].isalpha():
            base = "clock_" + base
        base = "on_" + base
        name = base
        serial = 1
        while name in used:
            serial += 1
            name = f"{base}_{serial}"
        used.add(name)
        return name

    def _gen_clock_event_semantics(
        self, ir_mod: IRModule, sequential_funcs: list[IRFunction], clocks: list[str],
    ) -> None:
        """Generate explicit event-indexed semantics for multiple clocks.

        Only processes belonging to the selected clock domain are committed.
        The other domains are copied from the same pre-event state, making
        cross-domain preservation structural rather than source-order based.
        """
        mod_name = ir_mod.name
        used: set[str] = set()
        constructors = {clock: self._clock_constructor(clock, used) for clock in clocks}
        self._doc("Explicit clock events discovered from the elaborated RTL")
        self._add("inductive ClockEvent where")
        self.indent_level += 1
        for clock in clocks:
            self._add(f"| {constructors[clock]}")
        self.indent_level -= 1
        self._add("deriving DecidableEq, Repr")
        self._blank()

        self._doc("Parallel commit selected by one explicit clock event")
        self._add(f"def commitEvent (s : {mod_name}State) (i : {mod_name}Inputs) "
                  f"(event : ClockEvent) : {mod_name}State :=")
        self.indent_level += 1
        self._add("match event with")
        self.indent_level += 1
        for clock in clocks:
            domain_funcs = [function for function in sequential_funcs if function.clock_signal == clock]
            self._add(f"| .{constructors[clock]} =>")
            self.indent_level += 1
            if len(domain_funcs) == 1:
                self._add(f"{domain_funcs[0].name} s i")
            else:
                self._gen_register_merge(ir_mod, domain_funcs, "s", "domain_next")
                self._add("domain_next")
            self.indent_level -= 1
        self.indent_level -= 1
        self.indent_level -= 1
        self._blank()

        self._doc("One hardware clock event: settle combinational logic, commit one domain, settle outputs")
        self._add(
            f"def stepEvent (s : {mod_name}State) (i : {mod_name}Inputs) "
            f"(event : ClockEvent) : {mod_name}State :="
        )
        self.indent_level += 1
        pre_state = "s"
        if self._has_comb:
            self._add("let s_pre := comb s i")
            pre_state = "s_pre"
        self._add(f"let s_next := commitEvent {pre_state} i event")
        if self._has_comb:
            self._add("let s_settled := comb s_next i")
            self._add("s_settled")
        else:
            self._add("s_next")
        self.indent_level -= 1
        self._blank()
        self._doc("Uniform multi-clock entry point")
        self._add(
            f"def step (s : {mod_name}State) (i : {mod_name}Inputs) "
            f"(event : ClockEvent) : {mod_name}State := stepEvent s i event"
        )
        self._blank()

    def _gen_bounded_comb_helpers(self, ir_mod: IRModule, comb_funcs: list) -> list:
        """Factor ordered composition only; preserve every state transition."""
        names = [func.name for func in comb_funcs]
        self._helper_names.update(func.name for func in ir_mod.functions)
        block_size = 32
        while len(names) > block_size:
            grouped = []
            for start in range(0, len(names), block_size):
                name = f"_rtl_comb_block_{self._helper_serial}"
                while name in self._helper_names:
                    self._helper_serial += 1
                    name = f"_rtl_comb_block_{self._helper_serial}"
                self._helper_serial += 1
                self._helper_names.add(name)
                self._add(f"private def {name} (s : {ir_mod.name}State) "
                          f"(i : {ir_mod.name}Inputs) : {ir_mod.name}State :=")
                self.indent_level += 1
                self._gen_comb_chain(names[start:start + block_size], "s", "result")
                self._add("result")
                self.indent_level -= 1
                self._blank()
                grouped.append(name)
            names = grouped
        return names

    def _gen_comb_chain(self, comb_funcs: list, input_state: str, output_name: str) -> None:
        """生成组合逻辑链：所有组合函数串联"""
        names = [func if isinstance(func, str) else func.name for func in comb_funcs]
        if len(comb_funcs) == 1:
            self._add(f"let {output_name} := {names[0]} {input_state} i")
        else:
            self._add(f"let {output_name} :=")
            self.indent_level += 1
            self._add(f"{names[0]} {input_state} i")
            for name in names[1:]:
                self._add(f"|> (fun s' => {name} s' i)")
            self.indent_level -= 1
            self._blank()

    def _gen_register_merge(
        self,
        ir_mod: IRModule,
        sequential_funcs: list,
        pre_state: str,
        result_name: str,
    ) -> None:
        """合并多个 always_ff 块的寄存器结果"""
        for idx, func in enumerate(sequential_funcs, start=1):
            self._add(f"let s{idx} := {func.name} {pre_state} i")

        reg_names = {n for n, _ in ir_mod.registers}
        reg_to_func = {}
        for idx, func in enumerate(sequential_funcs, start=1):
            for block in func.blocks:
                for instr in block.instrs:
                    if instr.kind in ("assign", "nba") and instr.dest in reg_names:
                        reg_to_func[instr.dest] = idx

        if not reg_to_func:
            self._add(f"let {result_name} := {pre_state}")
            return

        enum_names = {e[0] for e in ir_mod.enum_constants}
        self._add(f"let {result_name} := {{")
        self.indent_level += 1
        for name, _ in ir_mod.registers:
            if name in reg_to_func:
                self._add(f"{name} := s{reg_to_func[name]}.{name}")
            else:
                self._add(f"{name} := {pre_state}.{name}")
        for name, _ in ir_mod.signals:
            if name not in enum_names:
                self._add(f"{name} := {pre_state}.{name}")
        self.indent_level -= 1
        self._add("}")

    def _gen_output_function(self, ir_mod: IRModule) -> None:
        """生成输出助手函数"""
        output_ports = [(n, t) for n, t, is_in in ir_mod.ports if not is_in]
        if not output_ports:
            return
        self._doc(f"Output helper")
        self._add(f"def outputs (s : {ir_mod.name}State) : {ir_mod.name}Outputs :=")
        self.indent_level += 1
        self._add("{")
        self.indent_level += 1
        for name, _ in output_ports:
            self._add(f"{name} := s.{name}")
        self.indent_level -= 1
        self._add("}")
        self.indent_level -= 1
        self._blank()

    def _gen_instr(self, instr: IRInstr) -> None:
        """生成单条指令"""
        if instr.kind == "assign" and instr.dest and instr.value:
            self._add(f"let {instr.dest} := {self.ir_value_to_lean(instr.value)}")
        elif instr.kind == "nba" and instr.dest and instr.value:
            self._add(f"let {instr.dest}' := {self.ir_value_to_lean(instr.value)}")
        elif instr.kind == "return":
            if instr.value:
                self._add(self.ir_value_to_lean(instr.value))
            else:
                self._add("()")
        elif instr.kind == "label":
            self._comment(f"label: {instr.label}")
        elif instr.kind == "branch":
            self._comment(f"branch on {self.ir_value_to_lean(instr.cond)}")
        elif instr.kind == "jump":
            self._comment(f"jump to {instr.label}")

    def _gen_invariants(self, ir_mod: IRModule) -> None:
        """生成不变量（占位）"""
        pass

    def _gen_theorem_skeleton(self, ir_mod: IRModule) -> None:
        """生成定理骨架（占位 — 实际验证在 verifier 中）"""
        pass
