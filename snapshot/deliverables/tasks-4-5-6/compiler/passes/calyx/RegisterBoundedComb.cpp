// Share isolated pure computations without introducing combinational chains.
#include "circt/Dialect/Calyx/CalyxOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/SymbolTable.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/SmallVector.h"

using namespace mlir;
using namespace circt;

namespace {
struct Candidate { Operation *cell; calyx::GroupOp group; };

static bool marked(Operation *operation) {
  for (StringRef marker : {"external", "protected", "ref", "llm2fpga.runtime_port"})
    if (operation->hasAttr(marker)) return true;
  return false;
}

static calyx::AssignOp driver(Value port) {
  if (!port.hasOneUse()) return {};
  auto assignment = dyn_cast<calyx::AssignOp>(*port.getUsers().begin());
  return assignment && assignment.getDest() == port ? assignment : calyx::AssignOp{};
}

static calyx::AssignOp groupDriver(Value port, calyx::GroupOp group) {
  calyx::AssignOp found;
  for (auto &use : port.getUses()) {
    auto assignment = dyn_cast<calyx::AssignOp>(use.getOwner());
    // Other initialization/update writes are allowed only in dynamic groups.
    // No reads of input ports or continuously active captures are permitted.
    if (!assignment || assignment.getDest() != port ||
        !isa<calyx::GroupOp>(assignment->getParentOp())) return {};
    if (assignment->getParentOp() == group) {
      if (found) return {};
      found = assignment;
    }
  }
  return found;
}

static bool plainRegister(calyx::RegisterOp reg) {
  return reg && !marked(reg) && reg->getResult(2).use_empty() &&
         reg->getResult(3).use_empty();
}

static bool inspect(Operation *operation, calyx::ComponentOp component,
                    const llvm::DenseSet<StringAttr> &observed, Candidate &candidate) {
  if (!operation->hasTrait<calyx::Combinational>() || marked(operation)) return false;
  auto symbol = operation->getAttrOfType<StringAttr>(SymbolTable::getSymbolAttrName());
  if (!symbol || observed.contains(symbol)) return false;
  auto cell = dyn_cast<calyx::CellInterface>(operation);
  if (!cell || !cell.isCombinational()) return false;
  calyx::GroupOp group;
  Value output;
  unsigned inputCount = 0;
  for (auto [port, direction] : llvm::zip(operation->getResults(), cell.portDirections())) {
    if (direction == calyx::Direction::Input) {
      auto assignment = driver(port);
      if (!assignment || assignment.getGuard()) return false;
      auto scope = dyn_cast<calyx::GroupOp>(assignment->getParentOp());
      if (!scope || (group && group != scope)) return false;
      group = scope;
      Value source = assignment.getSrc();
      if (!source.getDefiningOp<hw::ConstantOp>()) {
        auto reg = source.getDefiningOp<calyx::RegisterOp>();
        if (!plainRegister(reg) || source != reg->getResult(4)) return false;
      }
      ++inputCount;
    } else {
      if (output) return false;
      output = port;
    }
  }
  if (!group || !inputCount || !output || !output.hasOneUse()) return false;
  auto captureInput = dyn_cast<calyx::AssignOp>(*output.getUsers().begin());
  if (!captureInput || captureInput.getSrc() != output || captureInput.getGuard() ||
      captureInput->getParentOp() != group) return false;
  auto capture = captureInput.getDest().getDefiningOp<calyx::RegisterOp>();
  if (!plainRegister(capture) || captureInput.getDest() != capture->getResult(0) ||
      groupDriver(capture->getResult(0), group) != captureInput) return false;
  if (observed.contains(capture->getAttrOfType<StringAttr>(SymbolTable::getSymbolAttrName())))
    return false;
  auto write = groupDriver(capture->getResult(1), group);
  if (!write || write.getGuard() || write->getParentOp() != group) return false;
  auto one = write.getSrc().getDefiningOp<hw::ConstantOp>();
  if (!one || one.getValue().getBitWidth() != 1 || !one.getValue().isOne()) return false;
  auto completion = dyn_cast<calyx::GroupDoneOp>(group.getBodyBlock()->getTerminator());
  if (!completion || completion->getNumOperands() != 1 ||
      completion->getOperand(0) != capture->getResult(5)) return false;
  bool exclusive = true, enabled = false;
  component.walk([&](calyx::EnableOp enable) {
    if (enable.getGroupNameAttr().getValue() != group.getSymName()) return;
    enabled = true;
    for (Operation *parent = enable->getParentOp(); parent != component.getOperation();
         parent = parent->getParentOp())
      if (!isa<calyx::SeqOp, calyx::IfOp, calyx::WhileOp, calyx::ControlOp>(parent))
        exclusive = false;
  });
  if (!enabled || !exclusive) return false;
  candidate = {operation, group};
  return true;
}

static DictionaryAttr signature(Operation *operation) {
  NamedAttrList attributes(operation->getAttrs());
  attributes.erase(SymbolTable::getSymbolAttrName());
  return attributes.getDictionary(operation->getContext());
}

struct RegisterBoundedComb
    : PassWrapper<RegisterBoundedComb, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(RegisterBoundedComb)
  StringRef getArgument() const final { return "llm2fpga-share-register-bounded-comb"; }
  StringRef getDescription() const final {
    return "Share pure register-fed and register-captured cells in exclusive dynamic groups";
  }
  void runOnOperation() override {
    // Observers can live on the enclosing module or a sibling component.
    // Preserve named cells/captures across every symbol-table scope, and stop
    // conservatively when an opaque observer cannot be inspected.
    llvm::DenseSet<StringAttr> observed;
    bool opaque = false;
    getOperation().walk([&](Operation *operation) {
      operation->getAttrDictionary().walk(
          [&](SymbolRefAttr reference) {
            observed.insert(reference.getRootReference());
            for (auto nested : reference.getNestedReferences()) observed.insert(nested.getAttr());
          }, [&](OpaqueAttr) { opaque = true; });
    });
    if (opaque) return;
    getOperation().walk([&](calyx::ComponentOp component) {
      SmallVector<Candidate> candidates;
      component.walk([&](Operation *operation) {
        Candidate candidate;
        if (inspect(operation, component, observed, candidate)) candidates.push_back(candidate);
      });
      // Plan from the original IR; merging ports creates multiple group drives,
      // deliberately making a merged cell ineligible on a repeated pass.
      for (size_t index = 0; index < candidates.size(); ++index) {
        auto &survivor = candidates[index];
        if (!survivor.cell) continue;
        llvm::DenseSet<Operation *> groups{survivor.group.getOperation()};
        for (size_t other = index + 1; other < candidates.size(); ++other) {
          auto &candidate = candidates[other];
          if (!candidate.cell || groups.contains(candidate.group.getOperation()) ||
              survivor.cell->getName() != candidate.cell->getName() ||
              survivor.cell->getResultTypes() != candidate.cell->getResultTypes() ||
              signature(survivor.cell) != signature(candidate.cell)) continue;
          groups.insert(candidate.group.getOperation());
          for (auto [from, to] : llvm::zip(candidate.cell->getResults(), survivor.cell->getResults()))
            from.replaceAllUsesWith(to);
          candidate.cell->erase();
          candidate.cell = nullptr;
        }
      }
    });
  }
};
}

void registerRegisterBoundedCombPass() { PassRegistration<RegisterBoundedComb>(); }
