# FIRRTL 到 Lean：实现、使用和验收

现在有两个入口：`chisel2lean.py` 保留支持范围内的源码参数；
`firrtl2lean.py` 读取已经展开的固定配置电路。两者共用硬件 IR v2、
Normalize、LeanEmitter 和 Lean Runtime。并行开发边界见
[firrtl-integration-coordination.md](firrtl-integration-coordination.md)。
Chisel 7.3 的现代文本现已接入固定版本 CIRCT，使用方法和新香山实验见
[现代展开入口](modern-firrtl.md)。下文的 Scala FIRRTL 流程指经典入口。

## 使用

已有项目工具安装完成后，构建独立 FIRRTL 前端：

```powershell
python scripts/sbt.py classicFirrtl/writeFirrtlClasspath
python scripts/firrtl2lean.py examples/firrtl/Counter4.fir --out generated/firrtl-demo
python scripts/firrtl_simulate.py generated/firrtl-demo/FIRRTL/Counter.ir.json --trace examples/firrtl/counter-trace.json --initial-state '{"cnt":0}' --evidence experiments/firrtl-demo-run
```

示例五拍的边沿后计数值为 `0, 1, 2, 2, 0`。初态由调用者显式给出；
这里的零只是实验条件，不是寄存器上电保证。组合电路使用
`--initial-state '{}'`。

FIRRTL 模型的 reset 是实际 `Inputs` 字段，轨迹中写
`{"inputs":{"reset":1,"io_en":1}}`。它与源码入口通过 `step` 的独立 Bool
参数传入复位的方式不同。FIRRTL 模型保留兼容的 step reset 参数，但不使用
该参数控制电路；调用一次 step 表示共同的时钟上升沿。

可以用 `--top 模块名` 选择电路中的模块，`--namespace 名称` 指定 Lean 命名
空间。默认先通过固定 Lean 4.19.0 类型检查，成功后才发布输出文件。
输出包含 `.lean`、`.ir.json`、`.olean`、官方规范化后的 `lowered.fir` 和
记录输入/编译器 SHA256 的 `manifest.json`。

## 转换方法

1. 官方 Scala FIRRTL 1.5.6 解析文本；兼容 CRLF，非法词法字符直接报错。
2. 官方 Low FIRRTL 流程推导位宽、展开 Bundle/Vec、处理 when 和连接优先级。
3. FirrtlAdapter 按实例路径处理模块连接，区分旧寄存器值和下一拍驱动，
   检查共同的时钟根，接入现有参数化 IR。这里的参数列表为空、宽度为常量。
4. 共用 Normalize 完成类型和驱动检查。FIRRTL 的 add/sub 保留进位，不能
   直接当作 Chisel 的非扩展加减；负数取负必须先扩展再计算。
5. FirrtlLeanEmitter 把表达式图拆成纯的具名 Lean 定义，避免巨大的单个项。
   每个定义读取相同的 Inputs 和旧 State。序列化的语义 IR 保持原表达式，
   未定义条件也沿原表达式传播。

GSIM 提供了 FIRRTL→AST→电路图→可执行程序的参考，以及寄存器当前值/
下一拍值分离的实现思路。本实现使用官方 FIRRTL 解析和规范化库，自行接入
现有 IR/Lean 后端；没有把 GSIM 的 C++ 仿真结果当作形式化证明。
参考：[GSIM 项目](https://github.com/OpenXiangShan/gsim)、
[GSIM 论文](https://talks-pubs.xiangshan.cc/publications/dac2025-GSIM.pdf)。

## 当前范围

支持定宽 UInt/SInt、常见算术/比较/位运算、转换、切片/拼接/扩展、
静态和动态移位、Mux、聚合类型的官方展开、多层模块及单时钟同步寄存器。
由寄存器组成的 FIFO 和寄存器文件已验证。

经典入口支持 FIRRTL 文本版本至 1.1.0（以及旧的无版本头格式）。
默认根据版本选择入口：现代文本由 CIRCT 解析和规范化，主要验收 Chisel 7.3
产生的 6.0.0；允许版本范围为 2.0.0 到 6.0.0，未来版本明确拒绝。
现代入口按 SSA 身份接入已有适配器，不使用经典解析器读取现代文本。
FIRRTL Mem/SyncReadMem、多时钟、门控时钟、异步复位、BlackBox/ExtModule、
assert/stop/printf 等尚未纳入本入口的语义范围，给出诊断，不生成假模型。
现代入口另外拒绝显式 DontCare/invalidvalue，避免官方规范化丢失未定义条件。
除零及 false validif 的观察会被执行器拒绝。构造参数、require 和生成分支
不能由固定电路自动恢复。

## 验收证据

`python scripts/verification/two_state/verify_firrtl_frontend.py` 比较新模型、已有源码模型及已归档的
独立原始 RTL 实验。原语用例另外运行新的官方 FIRRTL→Verilog→Verilator
路径和独立整数规格：22 个配置，34,155 拍，595,699 次输出比较；其中
PrimitiveWidths 穷举完整 15 位输入空间的 32,768 种组合。
详见 [validation-firrtl.json](validation-firrtl.json)。

`python scripts/verification/two_state/check_firrtl_contracts.py` 检查不支持结构、非法字符、新文本
版本、除零、false validif 和行为故障注入。故意把有符号取负输出改成零后，
模型仍通过类型检查，但行为比较检出错误；生产模型不受改动。
详见 [validation-firrtl-contracts.json](validation-firrtl-contracts.json)。

`python scripts/verification/two_state/verify_firrtl_bridges.py` 重新生成两端模型，并检查：

- 4 位 Counter：全部初态、所有使能/复位输入，以及任意长度的有限序列。
- 64 位 RightShift：全部数据位模式、所有六位移位量和两种复位值；同时
  检查历史适配实验与真实香山配置生成的模型。

这些是指定配置下两个生成模型之间的 Lean 等价证明。
不是整个转换器、官方工具链或所有构造参数的正确性证明。
详见 [validation-firrtl-bridges.json](validation-firrtl-bridges.json)。

## 真实香山工程

固定官方 v2.1 提交 `f0d8a1cf543c113433ff98140b6d77055d930deb`，递归下载
锁定的依赖，使用实际 `top.MinimalConfig(1)`，按核心配置绑定 XSCoreParamsKey。
运行原始 XSModule、参数框架和硬件类，没有 Parameters/XSModule 桩。
上游构建为 Chisel 3.5.3、Scala 2.12.13、Mill 0.9.8。

```powershell
python scripts/prepare_xiangshan_firrtl.py
python scripts/build_xiangshan_firrtl.py --module RightShiftModule
python scripts/build_xiangshan_firrtl.py --module AluDataModule
python scripts/verification/two_state/verify_xiangshan_firrtl.py
```

准备与构建需要联网。源码、依赖和工具位于本项目缓存；新增的唯一香山源码
是小型生成入口，原硬件类不改写。保存的 FIRRTL 和来源记录位于
`verification/xiangshan/firrtl`，可直接用转换入口读取。

RightShift 验证 1,408 拍、8,448 次输出比较。AluDataModule 含 10 个子模块
实例、710 个展开信号，覆盖全部 128 个操作码值，验证 25,600 拍、153,600
次输出比较，并完成 117,616 次独立整数检查。RTL 来自上游自己的 Chisel
生成路径。整数规格针对已定义的运算结果及分支信号，不把随机测试当成证明。
详见 [validation-xiangshan-firrtl.json](validation-xiangshan-firrtl.json)。

这个发布快照与之前 nanhu 的最新源码实验分别记录。最新 nanhu 已使用
Chisel 7.3，不能把 v2.1 的成功当作新版本或完整处理器的验收。
Chisel 7.3 的原始 AluDataModule、L2TlbPrefetch 已建立独立的现代入口验证，见
[现代入口说明](modern-firrtl.md)。存储器语义和更大模块仍待扩展。
