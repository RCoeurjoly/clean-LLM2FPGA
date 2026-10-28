// Export board-facing memory contracts from typed bound Calyx IR.
#include "circt/Dialect/Calyx/CalyxOps.h"
#include "mlir/IR/SymbolTable.h"
#include "mlir/Pass/Pass.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/raw_ostream.h"
#include <map>
#include <set>
using namespace mlir;
using namespace circt;
namespace {
struct ExternalMemoryInterface : PassWrapper<ExternalMemoryInterface, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(ExternalMemoryInterface)
  ExternalMemoryInterface() = default;
  ExternalMemoryInterface(const ExternalMemoryInterface &other) : PassWrapper(other) {}
  Option<std::string> interface{*this, "interface"};
  StringRef getArgument() const final { return "llm2fpga-export-external-memory-interface"; }
  StringRef getDescription() const final { return "Export validated typed external memory port contracts"; }
  void runOnOperation() override {
    auto module=getOperation();
    auto entry=module->getAttrOfType<StringAttr>("calyx.entrypoint");
    calyx::ComponentOp top;
    if (entry) module.walk([&](calyx::ComponentOp component) {
      if (component.getName()==entry.getValue()) top=component;
    });
    if (!top || interface.empty()) {
      module.emitError("external interface requires resolved calyx.entrypoint and output path");
      signalPassFailure(); return;
    }
    llvm::json::Array memories, ports;
    std::set<std::string> names;
    bool invalid=false;
    for (auto port : top.getPortInfo()) {
      auto type=dyn_cast<IntegerType>(port.type);
      if (!type || !names.insert(port.name.getValue().str()).second) { invalid=true; continue; }
      ports.push_back(llvm::json::Object{{"name",port.name.getValue().str()},
        {"width",static_cast<int64_t>(type.getWidth())},
        {"direction",port.direction==calyx::Input ? "input" : "output"}});
    }
    module.walk([&](calyx::PrimitiveOp cell) {
      auto external=cell->getAttrOfType<BoolAttr>("external");
      auto binding=cell->getAttrOfType<DictionaryAttr>("llm2fpga.binding");
      if (!external || !external.getValue() || !binding) return;
      auto ref=cell->getAttrOfType<BoolAttr>("llm2fpga.ref");
      auto width=binding.getAs<IntegerAttr>("width");
      auto depth=binding.getAs<IntegerAttr>("depth");
      auto address=binding.getAs<IntegerAttr>("address_width");
      auto implementation=binding.getAs<StringAttr>("implementation");
      auto writable=binding.getAs<BoolAttr>("writable");
      auto latency=binding.getAs<IntegerAttr>("read_latency");
      auto reject=[&]() {cell.emitError("bound external memory port contract mismatch"); invalid=true;};
      if (!ref || ref.getValue() || cell->getParentOfType<calyx::ComponentOp>()!=top ||
          !width || width.getInt()<1 || width.getInt()>64 || !depth || depth.getInt()<1 ||
          !address || address.getInt()<1 || address.getInt()>32 ||
          depth.getInt()>(int64_t(1)<<address.getInt()) || !implementation || !writable || !latency) {
        reject(); return;
      }
      std::map<std::string,std::pair<int64_t,calyx::Direction>> expected={
        {"addr0",{address.getInt(),calyx::Input}}, {"clk",{1,calyx::Input}},
        {"reset",{1,calyx::Input}}, {"content_en",{1,calyx::Input}},
        {"write_en",{1,calyx::Input}}, {"write_data",{width.getInt(),calyx::Input}},
        {"read_data",{width.getInt(),calyx::Output}}, {"done",{1,calyx::Output}}};
      auto cellName=SymbolTable::getSymbolName(cell).getValue().str();
      llvm::json::Array memoryPorts;
      auto typedPorts=cast<calyx::CellInterface>(cell.getOperation()).getPortInfo();
      if (typedPorts.size()!=expected.size()) {reject(); return;}
      for (auto port : typedPorts) {
        auto key=port.name.getValue().str();auto found=expected.find(key);
        auto type=dyn_cast<IntegerType>(port.type);
        if (found==expected.end() || !type || type.getWidth()!=found->second.first ||
            port.direction!=found->second.second) {reject(); return;}
        expected.erase(found);
        auto name=cellName+"_"+key;
        if (!names.insert(name).second) {reject(); return;}
        // Native externalization reverses cell-facing directions at the top.
        memoryPorts.push_back(llvm::json::Object{{"name",name},{"role",key},
          {"width",static_cast<int64_t>(type.getWidth())},
          {"direction",port.direction==calyx::Input ? "output" : "input"}});
      }
      llvm::json::Object record{{"component",top.getName().str()},{"cell",cellName},
        {"width",width.getInt()},{"depth",depth.getInt()},
        {"address_width",address.getInt()},{"implementation",implementation.getValue().str()},
        {"writable",writable.getValue()},{"read_latency",latency.getInt()},
        {"ports",std::move(memoryPorts)}};
      if (auto role=cell->getAttrOfType<StringAttr>("llm2fpga.runtime_port"))
        record["runtime_port"]=role.getValue().str();
      memories.push_back(std::move(record));
    });
    if (invalid) {signalPassFailure(); return;}
    std::error_code error;
    llvm::raw_fd_ostream output(interface,error,llvm::sys::fs::OF_Text);
    if (error) {module.emitError()<<"external interface output: "<<error.message();signalPassFailure();return;}
    output<<llvm::formatv("{0:2}\n",llvm::json::Value(llvm::json::Object{
      {"schema_version",1},{"entrypoint",entry.getValue().str()},
      {"ports",std::move(ports)},{"memories",std::move(memories)}}));
    output.flush();if (output.has_error()) signalPassFailure();
  }
};
}
void registerExternalMemoryInterfacePass() {PassRegistration<ExternalMemoryInterface>();}
