// CSE across native pure-cell ports, without multiplexing different operands.
#include "circt/Dialect/Calyx/CalyxOps.h"
#include "mlir/IR/SymbolTable.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/Hashing.h"

using namespace mlir;
using namespace circt;

namespace {
struct Inputs {
  SmallVector<Value> values;
  SmallVector<calyx::AssignOp> drivers;
};

static bool inspect(Operation *operation, Inputs &inputs,
                    const llvm::DenseSet<StringAttr> &symbols) {
  if (!operation->hasTrait<calyx::Combinational>()) return false;
  auto name = operation->getAttrOfType<StringAttr>(SymbolTable::getSymbolAttrName());
  if (!name || symbols.contains(name)) return false;
  for (StringRef marker : {"external", "protected", "ref", "llm2fpga.runtime_port"})
    if (operation->hasAttr(marker)) return false;
  auto cell = dyn_cast<calyx::CellInterface>(operation);
  if (!cell || !cell.isCombinational()) return false;
  for (auto [port, direction] : llvm::zip(operation->getResults(), cell.portDirections())) {
    if (direction == calyx::Direction::Input) {
      if (!port.hasOneUse()) return false;
      auto assignment = dyn_cast<calyx::AssignOp>(*port.getUsers().begin());
      if (!assignment || assignment.getDest() != port || assignment.getGuard() ||
          !isa<calyx::WiresOp>(assignment->getParentOp())) return false;
      inputs.values.push_back(assignment.getSrc());
      inputs.drivers.push_back(assignment);
    } else {
      for (auto &use : port.getUses())
        if (auto assignment = dyn_cast<calyx::AssignOp>(use.getOwner()))
          if (assignment.getDest() == port) return false;
    }
  }
  return !inputs.drivers.empty();
}

static DictionaryAttr signature(Operation *operation) {
  NamedAttrList attributes(operation->getAttrs());
  attributes.erase(SymbolTable::getSymbolAttrName());
  return attributes.getDictionary(operation->getContext());
}

struct IdenticalComb : PassWrapper<IdenticalComb, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(IdenticalComb)
  StringRef getArgument() const final { return "llm2fpga-deduplicate-identical-comb"; }
  StringRef getDescription() const final {
    return "Merge built-in pure cells with identical continuous SSA input drives";
  }
  void runOnOperation() override {
    getOperation().walk([&](calyx::ComponentOp component) {
      llvm::DenseSet<StringAttr> symbols;
      bool opaque = false;
      // SymbolTable use collection intentionally stops at nested tables.
      // Inspect all native attribute subelements, conservatively preserving a
      // matching name even when its precise nested scope differs. Unknown
      // opaque metadata cannot establish that a named cell is unobserved.
      component.walk([&](Operation *operation) {
        operation->getAttrDictionary().walk(
            [&](SymbolRefAttr reference) {
              symbols.insert(reference.getRootReference());
              for (auto nested : reference.getNestedReferences())
                symbols.insert(nested.getAttr());
            },
            [&](OpaqueAttr) { opaque = true; });
      });
      if (opaque) return;
      SmallVector<Operation *> cells;
      component.walk([&](Operation *operation) {
        Inputs inputs;
        if (inspect(operation, inputs, symbols)) cells.push_back(operation);
      });
      bool changed;
      do {
        changed = false;
        llvm::DenseMap<size_t, SmallVector<Operation *>> buckets;
        for (size_t index = 0; index < cells.size(); ++index) {
          Operation *candidate = cells[index];
          if (!candidate) continue;
          Inputs candidateInputs;
          if (!inspect(candidate, candidateInputs, symbols)) continue;
          auto key = llvm::hash_combine(candidate->getName().getStringRef(),
                                        signature(candidate).getAsOpaquePointer());
          for (Type type : candidate->getResultTypes())
            key = llvm::hash_combine(key, type.getAsOpaquePointer());
          for (Value value : candidateInputs.values)
            key = llvm::hash_combine(key, value.getAsOpaquePointer());
          auto &bucket = buckets[static_cast<size_t>(key)];
          bool merged = false;
          for (Operation *existing : bucket) {
            if (!existing || existing->getBlock() != candidate->getBlock() ||
                existing->getName() != candidate->getName() ||
                existing->getResultTypes() != candidate->getResultTypes() ||
                signature(existing) != signature(candidate)) continue;
            Inputs existingInputs;
            if (!inspect(existing, existingInputs, symbols) ||
                existingInputs.values != candidateInputs.values) continue;
            // Equal operation, properties, result types and exact SSA inputs
            // imply equal outputs at all times, including idle/X-valued inputs.
            // No activation mux, new dependency edge or lifetime reuse is added.
            for (auto driver : candidateInputs.drivers) driver.erase();
            auto cell = cast<calyx::CellInterface>(candidate);
            for (auto [oldPort, newPort, direction] : llvm::zip(
                     candidate->getResults(), existing->getResults(), cell.portDirections()))
              if (direction == calyx::Direction::Output)
                oldPort.replaceAllUsesWith(newPort);
            candidate->erase();
            cells[index] = nullptr;
            changed = true;
            merged = true;
            break;
          }
          if (!merged) bucket.push_back(candidate);
        }
      } while (changed);
    });
  }
};
}

void registerIdenticalCombPass() { PassRegistration<IdenticalComb>(); }
