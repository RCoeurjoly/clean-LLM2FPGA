// Remove group activation muxes only across an unobservable pure-cell boundary.
#include "circt/Dialect/Calyx/CalyxOps.h"
#include "mlir/IR/Dominance.h"
#include "mlir/IR/SymbolTable.h"
#include "llvm/ADT/DenseSet.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/SmallVector.h"

using namespace mlir;
using namespace circt;

namespace {
struct ExclusiveCombInputs
    : PassWrapper<ExclusiveCombInputs, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(ExclusiveCombInputs)
  StringRef getArgument() const final {
    return "llm2fpga-lift-exclusive-comb-inputs";
  }
  StringRef getDescription() const final {
    return "Lift unconditional drives of private pure cells out of one dynamic group";
  }
  void runOnOperation() override {
    DominanceInfo dominance(getOperation());
    // SSA uses do not describe named observers in metadata. Traverse native
    // attribute subelements across nested symbol tables, preserving any named
    // cell conservatively even when the reference has a different scope.
    llvm::DenseSet<StringAttr> observedSymbols;
    bool opaque = false;
    getOperation().walk([&](Operation *operation) {
      operation->getAttrDictionary().walk(
          [&](SymbolRefAttr reference) {
            observedSymbols.insert(reference.getRootReference());
            for (auto nested : reference.getNestedReferences())
              observedSymbols.insert(nested.getAttr());
          },
          [&](OpaqueAttr) { opaque = true; });
    });
    if (opaque) return;
    SmallVector<SmallVector<calyx::AssignOp>> plans;
    getOperation().walk([&](Operation *operation) {
      // This native trait identifies built-in pure library cells. Do not trust
      // arbitrary external primitive declarations or component annotations.
      if (!operation->hasTrait<calyx::Combinational>()) return;
      auto symbol = operation->getAttrOfType<StringAttr>(
          SymbolTable::getSymbolAttrName());
      if (!symbol || observedSymbols.contains(symbol)) return;
      // These markers can expose internal ports after native externalization,
      // or explicitly preserve the cell for observation. Missing IR readers
      // do not prove those ports unobservable. Reject marker presence even
      // when its value is false or has an unfamiliar attribute type.
      for (StringRef marker : {"external", "protected", "ref",
                               "llm2fpga.runtime_port"})
        if (operation->hasAttr(marker)) return;
      auto cell = dyn_cast<calyx::CellInterface>(operation);
      if (!cell || !cell.isCombinational()) return;
      auto directions = cell.portDirections();
      calyx::GroupOp scope;
      SmallVector<calyx::AssignOp> drives;
      for (auto [port, direction] : llvm::zip(operation->getResults(), directions)) {
        if (direction != calyx::Direction::Input) continue;
        // One unguarded driver, with no reads of an input port or alias uses.
        if (!port.hasOneUse()) return;
        auto assignment = dyn_cast<calyx::AssignOp>(*port.getUsers().begin());
        if (!assignment || assignment.getDest() != port || assignment.getGuard())
          return;
        auto group = dyn_cast<calyx::GroupOp>(assignment->getParentOp());
        if (!group || !isa<calyx::WiresOp>(group->getParentOp()) ||
            (scope && scope != group)) return;
        if (!dominance.dominates(assignment.getSrc(), group.getOperation()))
          return;
        scope = group;
        drives.push_back(assignment);
      }
      if (!scope || drives.empty()) return;
      bool observed = false;
      for (auto [port, direction] : llvm::zip(operation->getResults(), directions)) {
        if (direction != calyx::Direction::Output) continue;
        for (auto &use : port.getUses()) {
          auto assignment = dyn_cast<calyx::AssignOp>(use.getOwner());
          // In particular exclude public/continuous reads, other groups,
          // control conditions and group_done: their inactive value matters.
          if (!assignment || assignment->getParentOp() != scope.getOperation() ||
              assignment.getDest() == port) return;
          observed = true;
        }
      }
      if (observed) plans.push_back(std::move(drives));
    });
    // Analyze the original graph before changing any connected pure-cell
    // chains. Every observable group assignment retains its activation mask;
    // only intermediate pure computations may run while the group is idle.
    for (auto &drives : plans) {
      Operation *group = drives.front()->getParentOp();
      for (auto assignment : drives) assignment->moveBefore(group);
    }
  }
};
}

void registerExclusiveCombInputsPass() { PassRegistration<ExclusiveCombInputs>(); }
