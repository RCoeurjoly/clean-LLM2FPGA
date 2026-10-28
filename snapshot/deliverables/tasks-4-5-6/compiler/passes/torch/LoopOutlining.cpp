// Outline closed standard SCF loops; no model recognition or serialized edits.
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Dialect/SCF/IR/SCF.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/BuiltinOps.h"
#include "mlir/IR/IRMapping.h"
#include "mlir/IR/SymbolTable.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Transforms/RegionUtils.h"
#include "llvm/ADT/SetVector.h"
using namespace mlir;
namespace {
struct OutlineLoops : PassWrapper<OutlineLoops, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(OutlineLoops)
  StringRef getArgument() const final { return "llm2fpga-outline-private-loops"; }
  StringRef getDescription() const final { return "Outline closed SCF loops into private SSA closures"; }
  void runOnOperation() override {
    auto module = getOperation();
    SmallVector<func::FuncOp> functions(module.getOps<func::FuncOp>());
    unsigned serial = 0;
    for (auto function : functions) {
      if (function.isExternal() || !llvm::hasSingleElement(function.getBody()) ||
          function->hasAttr("llm2fpga.outlined_loop")) continue;
      SmallVector<scf::WhileOp> loops;
      for (auto loop : function.getBody().front().getOps<scf::WhileOp>())
        if (llvm::all_of(loop.getResults(), [](Value value) { return value.use_empty(); }))
          loops.push_back(loop);
      for (auto loop : loops) {
        llvm::SetVector<Value> captures;
        captures.insert(loop->operand_begin(), loop->operand_end());
        getUsedValuesDefinedAbove(loop->getRegions(), captures);
        SmallVector<Type> types;
        for (auto value : captures) types.push_back(value.getType());
        std::string name;
        do { name = "__llm2fpga_loop_" + std::to_string(serial++); }
        while (SymbolTable::lookupSymbolIn(module, name));
        OpBuilder builder(&getContext());
        builder.setInsertionPointToEnd(module.getBody());
        auto helper = builder.create<func::FuncOp>(loop.getLoc(), name,
            builder.getFunctionType(types, {}));
        helper.setPrivate();
        helper->setAttr("llm2fpga.outlined_loop", builder.getUnitAttr());
        auto *entry = helper.addEntryBlock();
        IRMapping mapped;
        for (auto [i, value] : llvm::enumerate(captures)) {
          mapped.map(value, entry->getArgument(i));
          if (auto argument = dyn_cast<BlockArgument>(value))
            if (argument.getOwner() == &function.getBody().front())
              helper.setArgAttrs(i, function.getArgAttrDict(argument.getArgNumber()));
        }
        builder.setInsertionPointToStart(entry);
        builder.clone(*loop, mapped);
        builder.create<func::ReturnOp>(loop.getLoc());
        builder.setInsertionPoint(loop);
        builder.create<func::CallOp>(loop.getLoc(), helper, captures.getArrayRef());
        loop.erase();
      }
    }
  }
};
}
namespace llm2fpga {
void registerLoopOutlining() {
  PassRegistration<OutlineLoops>();
}
}
