// Parse and merge context modules through MLIR's symbol table, never text edits.
#include "TypedTensor.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/IR/SymbolTable.h"
#include "mlir/Parser/Parser.h"
#include "mlir/Pass/Pass.h"
using namespace mlir;
namespace {
struct GenerationImport : PassWrapper<GenerationImport, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(GenerationImport)
  GenerationImport() = default;
  GenerationImport(const GenerationImport &other) : PassWrapper(other) {}
  ListOption<std::string> files{*this, "files", llvm::cl::MiscFlags::CommaSeparated};
  StringRef getArgument() const final { return "llm2fpga-import-generation"; }
  StringRef getDescription() const final {
    return "Import sixteen context modules with scoped symbol renaming";
  }
  void runOnOperation() override {
    auto destination = getOperation();
    auto fail = [&](StringRef message) {
      destination.emitError() << "generation import: " << message;
      signalPassFailure();
    };
    if (!destination.getBody()->empty() || !destination->getAttrs().empty()) {
      fail("requires an empty destination module"); return;
    }
    if (files.size() != 16) { fail("requires ordered context files 4 through 19"); return; }
    SmallVector<OwningOpRef<ModuleOp>> modules;
    for (auto [index, file] : llvm::enumerate(files)) {
      auto source = parseSourceFile<ModuleOp>(file, &getContext());
      if (!source) { fail("cannot parse context module"); return; }
      // Model weights are function arguments; no module-level globals or
      // unexamined module attributes are silently discarded by this importer.
      if (!source->getOperation()->getAttrs().empty()) {
        fail("unsupported context module attributes"); return;
      }
      auto entry = source->lookupSymbol<func::FuncOp>("main");
      if (!entry || entry.isDeclaration()) { fail("requires a defined main in each context"); return; }
      SmallVector<func::FuncOp> functions;
      for (Operation &operation : *source->getBody()) {
        auto function = dyn_cast<func::FuncOp>(operation);
        if (!function) { fail("only function definitions may be imported"); return; }
        if (function->hasAttr("llm2fpga.context_length") && function != entry) {
          fail("helper cannot declare a generation context"); return;
        }
        functions.push_back(function);
      }
      SymbolTable symbols(*source);
      std::string prefix = "generation_ctx_" + std::to_string(index + 4) + "_";
      for (auto function : functions) {
        std::string name = prefix + function.getSymName().str();
        if (failed(symbols.rename(function, name))) {
          fail("cannot rename context symbols"); return;
        }
        function.setPrivate();
      }
      entry->setAttr("llm2fpga.context_length",
        IntegerAttr::get(IntegerType::get(&getContext(), 64), index + 4));
      modules.push_back(std::move(source));
    }
    // Parsed and renamed all files before changing the destination module.
    for (auto &source : modules)
      for (Operation &operation : *source->getBody())
        destination.push_back(operation.clone());
  }
};
}
void llm2fpga::registerGenerationImport() { PassRegistration<GenerationImport>(); }
