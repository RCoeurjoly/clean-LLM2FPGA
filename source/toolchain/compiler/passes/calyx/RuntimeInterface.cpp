// Select physical runtime memories for native Calyx externalization.
#include "circt/Dialect/Calyx/CalyxOps.h"
#include "mlir/Pass/Pass.h"
#include "mlir/IR/SymbolTable.h"
#include <map>
#include <string>
using namespace mlir;
using namespace circt;
namespace {
struct RuntimeInterface : PassWrapper<RuntimeInterface, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(RuntimeInterface)
  StringRef getArgument() const final { return "llm2fpga-expose-runtime-memories"; }
  StringRef getDescription() const final {
    return "Validate and mark physical runtime buffers for native externalization";
  }
  void runOnOperation() override {
    auto module = getOperation();
    auto entrypoint = module->getAttrOfType<StringAttr>("calyx.entrypoint");
    if (!entrypoint) {
      module.emitError("runtime interface requires calyx.entrypoint");
      signalPassFailure(); return;
    }
    bool invalid = false;
    std::map<std::string, Operation *> physical;
    module.walk([&](calyx::PrimitiveOp cell) {
      auto attribute = cell->getAttr("llm2fpga.runtime_port");
      if (!attribute) return;
      auto role = dyn_cast<StringAttr>(attribute);
      auto ref = cell->getAttrOfType<BoolAttr>("llm2fpga.ref");
      if (!role || (role.getValue() != "prompt" &&
          role.getValue() != "generated_tokens" && role.getValue() != "last_logits") || !ref) {
        cell.emitError("invalid bound runtime memory metadata"); invalid = true; return;
      }
      if (ref.getValue()) return;
      auto binding = cell->getAttrOfType<DictionaryAttr>("llm2fpga.binding");
      auto width = binding ? binding.getAs<IntegerAttr>("width") : IntegerAttr();
      auto depth = binding ? binding.getAs<IntegerAttr>("depth") : IntegerAttr();
      auto writable = binding ? binding.getAs<BoolAttr>("writable") : BoolAttr();
      if (!width || width.getInt() != 64 || !depth || depth.getInt() <= 0 ||
          (role.getValue() == "prompt" && depth.getInt() != 4) ||
          (role.getValue() == "generated_tokens" && depth.getInt() != 16) ||
          !writable || !writable.getValue()) {
        cell.emitError("invalid runtime memory geometry or mutability"); invalid = true; return;
      }
      auto component = cell->getParentOfType<calyx::ComponentOp>();
      if (!component || component.getName() != entrypoint.getValue()) {
        cell.emitError("physical runtime memory must belong to entrypoint");
        invalid = true; return;
      }
      if (!physical.emplace(role.getValue().str(), cell.getOperation()).second) {
        cell.emitError("duplicate physical runtime role"); invalid = true;
      }
    });
    for (StringRef role : {"prompt", "generated_tokens", "last_logits"})
      if (!physical.count(role.str())) {
        module.emitError() << "missing physical runtime role: " << role; invalid = true;
      }
    for (auto &[role, cell] : physical) {
      auto component = cell->getParentOfType<calyx::ComponentOp>();
      auto existing = SymbolTable::lookupSymbolIn(component, role);
      if (existing && existing != cell) {
        cell->emitError("runtime interface name collision"); invalid = true;
      }
    }
    if (invalid) { signalPassFailure(); return; }
    for (auto &[role, cell] : physical) {
      auto component = cell->getParentOfType<calyx::ComponentOp>();
      auto name = StringAttr::get(&getContext(), role);
      auto previous = SymbolTable::getSymbolName(cell);
      // Calyx's invocation maps are cell references, not generic symbol uses.
      component.walk([&](calyx::InvokeOp invoke) {
        SmallVector<Attribute> mappings;
        for (auto attribute : invoke.getRefCellsMap()) {
          auto mapping = cast<DictionaryAttr>(attribute);
          NamedAttrList entries(mapping);
          for (auto entry : mapping) {
            auto source = dyn_cast<FlatSymbolRefAttr>(entry.getValue());
            if (source && source.getValue() == previous.getValue())
              entries.set(entry.getName(), FlatSymbolRefAttr::get(name));
          }
          mappings.push_back(entries.getDictionary(&getContext()));
        }
        invoke.setRefCellsMapAttr(ArrayAttr::get(&getContext(), mappings));
      });
      if (failed(SymbolTable::replaceAllSymbolUses(cell, name, component))) {
        cell->emitError("cannot rename runtime interface symbol uses");
        signalPassFailure(); return;
      }
      SymbolTable::setSymbolName(cell, name);
      cell->setAttr("external", BoolAttr::get(&getContext(), true));
    }
  }
};
}
void registerRuntimeInterfacePass() { PassRegistration<RuntimeInterface>(); }
