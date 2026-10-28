// Serialize typed memory geometry and invocation edges without parsing IR text.
#include "circt/Dialect/Calyx/CalyxOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/Pass/Pass.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/raw_ostream.h"
using namespace mlir;
using namespace circt;
namespace {
struct MemoryInventory : PassWrapper<MemoryInventory, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(MemoryInventory)
  MemoryInventory() = default;
  MemoryInventory(const MemoryInventory &other) : PassWrapper(other) {}
  Option<std::string> inventory{*this, "inventory"};
  StringRef getArgument() const final { return "llm2fpga-export-memory-inventory"; }
  StringRef getDescription() const final { return "Serialize typed memory cells and reference edges"; }
  void runOnOperation() override {
    llvm::json::Array memories, references;
    bool invalid = false;
    getOperation().walk([&](calyx::SeqMemoryOp memory) {
      if (memory.getSizes().size() != 1 || memory.getAddrSizes().size() != 1) {
        memory.emitError("memory inventory requires flattened geometry");
        invalid = true; return;
      }
      bool disabled = true;
      for (auto &use : memory.writeEn().getUses()) {
        auto assignment = dyn_cast<calyx::AssignOp>(use.getOwner());
        if (!assignment || assignment.getDest() != memory.writeEn()) {
          disabled = false; break;
        }
        auto constant = assignment.getSrc().getDefiningOp<hw::ConstantOp>();
        if (!constant || !constant.getValue().isZero()) { disabled = false; break; }
      }
      auto component = memory->getParentOfType<calyx::ComponentOp>();
      llvm::json::Object record{
        {"component", component.getName().str()}, {"cell", memory.instanceName().str()},
        {"width", static_cast<int64_t>(memory.getWidth())},
        {"depth", cast<IntegerAttr>(memory.getSizes()[0]).getInt()},
        {"address_width", cast<IntegerAttr>(memory.getAddrSizes()[0]).getInt()},
        {"physical", memory->hasAttr("external")}, {"writes_disabled", disabled}};
      if (auto origin = memory->getAttrOfType<DictionaryAttr>("llm2fpga.input_origin")) {
        llvm::json::Object serialized;
        for (auto entry : origin) {
          if (auto text = dyn_cast<StringAttr>(entry.getValue()))
            serialized[entry.getName().str()] = text.getValue().str();
          else if (auto number = dyn_cast<IntegerAttr>(entry.getValue()))
            serialized[entry.getName().str()] = number.getInt();
          else {
            memory.emitError("unsupported graph input origin attribute");
            invalid = true; return;
          }
        }
        record["origin"] = std::move(serialized);
      }
      if (auto role = memory->getAttrOfType<StringAttr>("llm2fpga.runtime_port"))
        record["runtime_port"] = role.getValue().str();
      memories.push_back(std::move(record));
    });
    getOperation().walk([&](calyx::InvokeOp invoke) {
      auto caller = invoke->getParentOfType<calyx::ComponentOp>();
      auto instance = caller.lookupSymbol<calyx::InstanceOp>(invoke.getCallee());
      if (invoke.getRefCellsMap().empty()) return;
      if (!instance || !instance.getReferencedComponent()) {
        invoke.emitError("memory inventory requires a resolved component invocation");
        invalid = true; return;
      }
      for (auto attribute : invoke.getRefCellsMap()) {
        auto mapping = dyn_cast<DictionaryAttr>(attribute);
        if (!mapping) { invalid = true; return; }
        for (auto entry : mapping) {
          auto source = dyn_cast<FlatSymbolRefAttr>(entry.getValue());
          if (!source) { invalid = true; return; }
          references.push_back(llvm::json::Object{
            {"caller", caller.getName().str()},
            {"callee", instance.getReferencedComponent().getName().str()},
            {"source", source.getValue().str()}, {"destination", entry.getName().str()}});
        }
      }
    });
    if (invalid) { signalPassFailure(); return; }
    std::error_code error;
    llvm::raw_fd_ostream output(inventory, error, llvm::sys::fs::OF_Text);
    if (error) {
      getOperation().emitError() << "memory inventory output: " << error.message();
      signalPassFailure(); return;
    }
    output << llvm::formatv("{0:2}\n", llvm::json::Value(llvm::json::Object{
      {"schema_version", 1}, {"memories", std::move(memories)},
      {"references", std::move(references)}}));
    output.flush();
    if (output.has_error()) { signalPassFailure(); return; }
  }
};
}
void registerMemoryInventoryPass() { PassRegistration<MemoryInventory>(); }
