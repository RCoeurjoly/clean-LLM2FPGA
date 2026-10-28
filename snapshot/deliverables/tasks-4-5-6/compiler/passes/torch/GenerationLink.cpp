// Link graph-derived, bufferized step functions with explicit runtime feedback.
// Computation stays inside the imported functions; this pass adds API control.
#include "TypedTensor.h"
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Dialect/MemRef/IR/MemRef.h"
#include "mlir/Dialect/SCF/IR/SCF.h"
#include "mlir/Pass/Pass.h"
using namespace mlir;
namespace {
struct Step {
  func::FuncOp function;
  unsigned tokens, token, logits;
  SmallVector<unsigned> frozen;
};
struct GenerationLink : PassWrapper<GenerationLink, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(GenerationLink)
  StringRef getArgument() const final { return "llm2fpga-link-generation"; }
  StringRef getDescription() const final {
    return "Link contexts 4 through 19 using computed token feedback";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<arith::ArithDialect, func::FuncDialect,
                    memref::MemRefDialect, scf::SCFDialect>();
  }
  void runOnOperation() override {
    auto module = getOperation();
    auto fail = [&](StringRef message) {
      module.emitError() << "generation link: " << message;
      signalPassFailure();
    };
    if (module.lookupSymbol("main")) { fail("main already exists"); return; }
    SmallVector<func::FuncOp> functions;
    for (auto function : module.getOps<func::FuncOp>())
      if (function->hasAttr("llm2fpga.context_length")) functions.push_back(function);
    if (functions.size() != 16) { fail("requires ordered contexts 4 through 19"); return; }
    SmallVector<Step> steps;
    MemRefType logitsType;
    auto i64 = IntegerType::get(&getContext(), 64);
    for (auto [index, function] : llvm::enumerate(functions)) {
      auto length = function->getAttrOfType<IntegerAttr>("llm2fpga.context_length");
      if (!length || length.getInt() != static_cast<int64_t>(index + 4)) {
        fail("requires ordered contexts 4 through 19"); return;
      }
      if (function.isDeclaration() || function.getNumResults() != 0) {
        fail("requires defined out-parameter step functions"); return;
      }
      Step step{function, 0, 0, 0, {}};
      SmallVector<unsigned> runtime, outputs;
      for (unsigned arg = 0; arg < function.getNumArguments(); ++arg) {
        auto type = dyn_cast<MemRefType>(function.getArgument(arg).getType());
        if (!type || !type.hasStaticShape() || type.getElementType() != i64 ||
            !type.getLayout().isIdentity() || type.getMemorySpace()) {
          fail("requires static identity-layout i64 buffers"); return;
        }
        auto origin = function.getArgAttrOfType<DictionaryAttr>(arg, "llm2fpga.input_origin");
        bool output = function.getArgAttr(arg, "bufferize.result") != nullptr;
        if (output && origin) { fail("output cannot alias an input contract"); return; }
        if (output) { outputs.push_back(arg); continue; }
        auto kind = origin ? origin.getAs<StringAttr>("kind") : StringAttr();
        if (!kind) { fail("unclassified function argument"); return; }
        if (kind.getValue() == "USER_INPUT") runtime.push_back(arg);
        else if (kind.getValue() == "BUFFER" || kind.getValue() == "PARAMETER" ||
                 kind.getValue() == "CONSTANT_TENSOR") {
          auto hash = origin.getAs<StringAttr>("sha256");
          if (!hash || hash.getValue().size() != 64 || !origin.getAs<StringAttr>("target")) {
            fail("frozen input contract identity required"); return;
          }
          step.frozen.push_back(arg);
        } else { fail("unsupported input origin"); return; }
      }
      if (runtime.size() != 1 || outputs.size() != 2) {
        fail("requires one runtime input and two marked outputs"); return;
      }
      step.tokens = runtime[0]; step.token = outputs[0]; step.logits = outputs[1];
      auto tokensType = cast<MemRefType>(function.getArgument(step.tokens).getType());
      if (tokensType.getShape() != ArrayRef<int64_t>({1, static_cast<int64_t>(index + 4)})) {
        fail("runtime context geometry mismatch"); return;
      }
      auto tokenType = cast<MemRefType>(function.getArgument(step.token).getType());
      auto currentLogits = cast<MemRefType>(function.getArgument(step.logits).getType());
      if (tokenType.getRank() != 0 || currentLogits.getRank() != 1 ||
          currentLogits.getDimSize(0) <= 0 || (logitsType && currentLogits != logitsType)) {
        fail("token/logits output geometry mismatch"); return;
      }
      logitsType = currentLogits;
      if (!steps.empty()) {
        auto &first = steps.front();
        if (step.frozen.size() != first.frozen.size()) {
          fail("frozen input contract mismatch"); return;
        }
        for (auto [position, arg] : llvm::enumerate(step.frozen)) {
          auto firstArg = first.frozen[position];
          if (function.getArgument(arg).getType() != first.function.getArgument(firstArg).getType() ||
              function.getArgAttr(arg, "llm2fpga.input_origin") !=
                first.function.getArgAttr(firstArg, "llm2fpga.input_origin")) {
            fail("frozen input contract mismatch"); return;
          }
        }
      }
      steps.push_back(step);
    }
    // Validate the whole domain before adding any controller operations.
    OpBuilder builder(&getContext());
    auto location = module.getLoc();
    SmallVector<Type> arguments;
    for (unsigned arg : steps.front().frozen)
      arguments.push_back(steps.front().function.getArgument(arg).getType());
    unsigned promptIndex = arguments.size();
    arguments.push_back(MemRefType::get({1, 4}, i64));
    arguments.push_back(MemRefType::get({16}, i64));
    arguments.push_back(logitsType);
    auto main = func::FuncOp::create(location, "main", builder.getFunctionType(arguments, {}));
    module.push_back(main);
    for (auto [index, arg] : llvm::enumerate(steps.front().frozen))
      main.setArgAttr(index, "llm2fpga.input_origin",
                     steps.front().function.getArgAttr(arg, "llm2fpga.input_origin"));
    main.setArgAttr(promptIndex, "llm2fpga.input_origin",
      steps.front().function.getArgAttr(steps.front().tokens, "llm2fpga.input_origin"));
    main.setArgAttr(promptIndex + 1, "bufferize.result", builder.getUnitAttr());
    main.setArgAttr(promptIndex + 2, "bufferize.result", builder.getUnitAttr());
    main.setArgAttr(promptIndex, "llm2fpga.runtime_port", builder.getStringAttr("prompt"));
    main.setArgAttr(promptIndex + 1, "llm2fpga.runtime_port",
                    builder.getStringAttr("generated_tokens"));
    main.setArgAttr(promptIndex + 2, "llm2fpga.runtime_port",
                    builder.getStringAttr("last_logits"));
    main->setAttr("llm2fpga.generation_steps", builder.getI64IntegerAttr(16));
    main->setAttr("llvm.emit_c_interface", builder.getUnitAttr());
    builder.setInsertionPointToStart(main.addEntryBlock());
    auto constant = [&](int64_t value) -> Value {
      return builder.create<arith::ConstantIndexOp>(location, value);
    };
    Value zero = constant(0), one = constant(1), four = constant(4);
    // The hardware backend maps these private buffers to persistent memories;
    // allocation has no runtime heap/free protocol in this compilation route.
    auto context = builder.create<memref::AllocOp>(location, MemRefType::get({1, 20}, i64));
    auto token = builder.create<memref::AllocOp>(location, MemRefType::get({}, i64));
    auto copyPrefix = [&](Value source, Value target, Value count) {
      auto loop = builder.create<scf::ForOp>(location, zero, count, one);
      OpBuilder::InsertionGuard guard(builder);
      builder.setInsertionPointToStart(loop.getBody());
      Value index = loop.getInductionVar();
      auto value = builder.create<memref::LoadOp>(location, source, ValueRange{zero, index});
      builder.create<memref::StoreOp>(location, value, target, ValueRange{zero, index});
    };
    copyPrefix(main.getArgument(promptIndex), context, four);
    for (auto [index, step] : llvm::enumerate(steps)) {
      Value length = constant(index + 4), generatedIndex = constant(index);
      auto prefix = builder.create<memref::AllocOp>(location,
        cast<MemRefType>(step.function.getArgument(step.tokens).getType()));
      copyPrefix(context, prefix, length);
      SmallVector<Value> operands(step.function.getNumArguments());
      for (auto [position, arg] : llvm::enumerate(step.frozen))
        operands[arg] = main.getArgument(position);
      operands[step.tokens] = prefix;
      operands[step.token] = token;
      operands[step.logits] = main.getArgument(promptIndex + 2);
      builder.create<func::CallOp>(location, step.function.getSymName(), TypeRange{}, operands);
      // A synchronous call completes before its output is read or appended.
      auto computed = builder.create<memref::LoadOp>(location, token, ValueRange{});
      builder.create<memref::StoreOp>(location, computed, context, ValueRange{zero, length});
      builder.create<memref::StoreOp>(location, computed,
        main.getArgument(promptIndex + 1), ValueRange{generatedIndex});
    }
    builder.create<func::ReturnOp>(location);
  }
};
}
void llm2fpga::registerGenerationLink() { PassRegistration<GenerationLink>(); }
