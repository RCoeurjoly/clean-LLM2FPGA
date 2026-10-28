// Resolve semantic runtime buffers without guessing generated cell names.
#include "circt/Dialect/Calyx/CalyxOps.h"
#include "mlir/Pass/Pass.h"
#include <map>
#include <set>
#include <string>
#include <vector>
using namespace mlir;
using namespace circt;
namespace {
struct RuntimeMemories : PassWrapper<RuntimeMemories, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(RuntimeMemories)
  StringRef getArgument() const final { return "llm2fpga-resolve-runtime-memories"; }
  StringRef getDescription() const final {
    return "Resolve generation runtime roles through invocation memory references";
  }
  void runOnOperation() override {
    auto module = getOperation();
    auto entrypoint = module->getAttrOfType<StringAttr>("calyx.entrypoint");
    if (!entrypoint) {
      module.emitError("runtime memory resolution requires calyx.entrypoint");
      signalPassFailure(); return;
    }
    bool invalid = false;
    std::map<Operation *, std::vector<Operation *>> callers;
    module.walk([&](calyx::InvokeOp invoke) {
      auto caller = invoke->getParentOfType<calyx::ComponentOp>();
      auto instance = caller.lookupSymbol<calyx::InstanceOp>(invoke.getCallee());
      if (invoke.getRefCellsMap().empty()) return;
      if (!instance || !instance.getReferencedComponent()) {
        invoke.emitError("unresolved runtime memory invocation"); invalid = true; return;
      }
      auto callee = dyn_cast<calyx::ComponentOp>(
          instance.getReferencedComponent().getOperation());
      if (!callee) {
        invoke.emitError("runtime memory references require a sequential component");
        invalid = true; return;
      }
      for (auto attribute : invoke.getRefCellsMap()) {
        auto mapping = dyn_cast<DictionaryAttr>(attribute);
        if (!mapping) { invalid = true; return; }
        for (auto entry : mapping) {
          auto source = dyn_cast<FlatSymbolRefAttr>(entry.getValue());
          auto destination = callee.lookupSymbol<calyx::SeqMemoryOp>(entry.getName());
          auto memory = source ? caller.lookupSymbol<calyx::SeqMemoryOp>(source.getValue())
                               : calyx::SeqMemoryOp();
          if (!memory || !destination) {
            invoke.emitError("runtime memory references require unbound memory cells");
            invalid = true; return;
          }
          callers[destination.getOperation()].push_back(memory.getOperation());
        }
      }
    });
    std::map<std::string, Operation *> resolved;
    module.walk([&](calyx::SeqMemoryOp memory) {
      auto attribute = memory->getAttr("llm2fpga.runtime_port");
      if (!attribute) return;
      auto role = dyn_cast<StringAttr>(attribute);
      if (!role || (role.getValue() != "prompt" && role.getValue() != "generated_tokens" &&
                    role.getValue() != "last_logits")) {
        memory.emitError("unknown runtime role"); invalid = true; return;
      }
      std::vector<Operation *> pending{memory.getOperation()};
      std::set<Operation *> visited, physical;
      while (!pending.empty()) {
        auto current = pending.back(); pending.pop_back();
        if (!visited.insert(current).second) continue;
        if (current->hasAttr("external")) physical.insert(current);
        else for (auto source : callers[current]) pending.push_back(source);
      }
      if (physical.size() != 1) {
        memory.emitError("runtime role requires exactly one physical memory");
        invalid = true; return;
      }
      auto target = *physical.begin();
      if (target->getParentOfType<calyx::ComponentOp>().getName() != entrypoint.getValue()) {
        memory.emitError("runtime memory must belong to the entrypoint");
        invalid = true; return;
      }
      auto [position, inserted] = resolved.emplace(role.getValue().str(), target);
      if (!inserted && position->second != target) {
        memory.emitError("runtime role resolves to multiple physical memories");
        invalid = true;
      }
    });
    std::set<Operation *> distinct;
    for (StringRef role : {"prompt", "generated_tokens", "last_logits"}) {
      auto position = resolved.find(role.str());
      if (position == resolved.end()) {
        module.emitError() << "missing runtime role: " << role; invalid = true;
      } else if (!distinct.insert(position->second).second) {
        module.emitError("conflicting runtime roles on one physical memory"); invalid = true;
      }
    }
    if (invalid) { signalPassFailure(); return; }
    for (auto &[role, memory] : resolved)
      memory->setAttr("llm2fpga.runtime_port", StringAttr::get(&getContext(), role));
  }
};
}
void registerRuntimeMemoryPass() { PassRegistration<RuntimeMemories>(); }
