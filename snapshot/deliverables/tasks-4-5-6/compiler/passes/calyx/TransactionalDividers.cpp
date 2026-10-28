// Reuse only canonical, exclusively activated, register-captured transactions.
#include "circt/Dialect/Calyx/CalyxOps.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/IR/SymbolTable.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringSet.h"

using namespace mlir;
using namespace circt;

namespace {
struct Transaction {
  Operation *cell;
  calyx::GroupOp group;
  calyx::RegisterOp capture;
  SmallVector<calyx::EnableOp> enables;
};

static bool one(Value value) {
  auto constant = value.getDefiningOp<hw::ConstantOp>();
  return constant && constant.getValue().getBitWidth() == 1 && constant.getValue().isOne();
}

static calyx::AssignOp driver(Value port) {
  if (!port.hasOneUse()) return {};
  auto assignment = dyn_cast<calyx::AssignOp>(*port.getUsers().begin());
  return assignment && assignment.getDest() == port ? assignment : calyx::AssignOp{};
}

static bool inspect(Operation *cell, calyx::ComponentOp component,
                    const llvm::DenseSet<StringAttr> &observed, Transaction &transaction) {
  if (!isa<calyx::DivSPipeLibOp, calyx::RemSPipeLibOp>(cell) || cell->getNumResults() != 7)
    return false;
  for (StringRef marker : {"external", "protected", "ref", "llm2fpga.runtime_port",
                           "llm2fpga.divider_shared"})
    if (cell->hasAttr(marker)) return false;
  if (observed.contains(cell->getAttrOfType<StringAttr>(SymbolTable::getSymbolAttrName())))
    return false;
  // Clock/reset are connected by native Calyx serialization, never overridden.
  if (!cell->getResult(0).use_empty() || !cell->getResult(1).use_empty()) return false;
  auto left = driver(cell->getResult(3)), right = driver(cell->getResult(4));
  auto go = driver(cell->getResult(2));
  if (!left || !right || !go || left.getGuard() || right.getGuard()) return false;
  auto group = dyn_cast<calyx::GroupOp>(left->getParentOp());
  if (!group || right->getParentOp() != group || go->getParentOp() != group) return false;
  Value done = cell->getResult(6);
  auto inverse = go.getGuard().getDefiningOp<comb::XorOp>();
  if (!one(go.getSrc()) || !inverse || inverse->getParentOp() != group ||
      inverse->getNumOperands() != 2 ||
      !((inverse->getOperand(0) == done && one(inverse->getOperand(1))) ||
        (inverse->getOperand(1) == done && one(inverse->getOperand(0))))) return false;
  Value output = cell->getResult(5);
  if (!output.hasOneUse()) return false;
  auto captureInput = dyn_cast<calyx::AssignOp>(*output.getUsers().begin());
  if (!captureInput || captureInput.getSrc() != output || captureInput.getGuard() ||
      captureInput->getParentOp() != group) return false;
  auto capture = captureInput.getDest().getDefiningOp<calyx::RegisterOp>();
  if (!capture || captureInput.getDest() != capture->getResult(0)) return false;
  if (!capture->getResult(2).use_empty() || !capture->getResult(3).use_empty()) return false;
  for (StringRef marker : {"external", "protected", "ref", "llm2fpga.runtime_port"})
    if (capture->hasAttr(marker)) return false;
  if (observed.contains(capture->getAttrOfType<StringAttr>(SymbolTable::getSymbolAttrName())))
    return false;
  auto write = driver(capture->getResult(1));
  if (!write || write.getSrc() != done || write.getGuard() || write->getParentOp() != group)
    return false;
  auto completion = dyn_cast<calyx::GroupDoneOp>(group.getBodyBlock()->getTerminator());
  if (!completion || completion->getNumOperands() != 1 ||
      completion->getOperand(0) != capture->getResult(5)) return false;
  // No other read of divider completion can escape the captured transaction.
  for (Operation *user : done.getUsers())
    if (user != inverse.getOperation() && user != write.getOperation()) return false;
  SmallVector<calyx::EnableOp> enables;
  bool exclusive = true;
  component.walk([&](calyx::EnableOp enable) {
    if (enable.getGroupNameAttr().getValue() != group.getSymName()) return;
    enables.push_back(enable);
    for (Operation *parent = enable->getParentOp(); parent != component.getOperation();
         parent = parent->getParentOp()) {
      if (!isa<calyx::SeqOp, calyx::IfOp, calyx::WhileOp, calyx::ControlOp>(parent))
        exclusive = false;
    }
  });
  if (!exclusive || enables.empty()) return false;
  transaction = {cell, group, capture, std::move(enables)};
  return true;
}

static DictionaryAttr signature(Operation *cell) {
  NamedAttrList attributes(cell->getAttrs());
  attributes.erase(SymbolTable::getSymbolAttrName());
  return attributes.getDictionary(cell->getContext());
}

struct TransactionalDividers
    : PassWrapper<TransactionalDividers, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(TransactionalDividers)
  StringRef getArgument() const final { return "llm2fpga-share-transactional-dividers"; }
  StringRef getDescription() const final {
    return "Reuse exclusively activated signed dividers with captured results and retirement";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<calyx::CalyxDialect, comb::CombDialect, hw::HWDialect>();
  }
  void runOnOperation() override {
    struct Plan { SmallVector<Transaction> transactions; };
    SmallVector<Plan> plans;
    for (auto component : getOperation().getOps<calyx::ComponentOp>()) {
      llvm::DenseSet<StringAttr> observed;
      bool opaque = false;
      component.walk([&](Operation *operation) {
        operation->getAttrDictionary().walk(
            [&](SymbolRefAttr reference) {
              observed.insert(reference.getRootReference());
              for (auto nested : reference.getNestedReferences()) observed.insert(nested.getAttr());
            }, [&](OpaqueAttr) { opaque = true; });
      });
      if (opaque) continue;
      SmallVector<Plan> componentPlans;
      component.walk([&](Operation *cell) {
        Transaction transaction;
        if (!inspect(cell, component, observed, transaction)) return;
        for (auto &plan : componentPlans) {
          Operation *existing = plan.transactions.front().cell;
          if (existing->getName() == cell->getName() &&
              existing->getResultTypes() == cell->getResultTypes() &&
              signature(existing) == signature(cell)) {
            plan.transactions.push_back(std::move(transaction));
            return;
          }
        }
        componentPlans.push_back({{std::move(transaction)}});
      });
      llvm::StringSet<> names;
      component.walk([&](calyx::GroupOp group) { names.insert(group.getSymName()); });
      for (auto &plan : componentPlans) {
        if (plan.transactions.size() < 2) continue;
        for (auto &transaction : plan.transactions) {
          auto name = transaction.group.getSymName().str() + "_divider_retire";
          if (!names.insert(name).second) {
            transaction.group.emitError("divider retirement group name collision");
            signalPassFailure(); return;
          }
        }
        plans.push_back(std::move(plan));
      }
    }
    // All eligibility and collision checks precede mutation.
    for (auto &plan : plans) {
      Operation *survivor = plan.transactions.front().cell;
      Value done = survivor->getResult(6);
      for (auto &transaction : plan.transactions) {
        auto group = transaction.group;
        OpBuilder builder(group);
        Value trueValue = hw::ConstantOp::create(builder, group.getLoc(), builder.getI1Type(), 1);
        std::string name = group.getSymName().str() + "_divider_retire";
        auto retirement = calyx::GroupOp::create(builder, group.getLoc(), name);
        builder.setInsertionPointToStart(retirement.getBodyBlock());
        Value retired = comb::XorOp::create(builder, group.getLoc(), ValueRange{done, trueValue}, false);
        Value captureRetired = comb::XorOp::create(builder, group.getLoc(),
            ValueRange{transaction.capture->getResult(5), trueValue}, false);
        Value ready = comb::AndOp::create(builder, group.getLoc(),
            ValueRange{retired, captureRetired}, false);
        calyx::GroupDoneOp::create(builder, group.getLoc(), trueValue, ready);
        for (auto enable : transaction.enables) {
          builder.setInsertionPoint(enable);
          auto sequence = calyx::SeqOp::create(builder, enable.getLoc());
          builder.setInsertionPointToEnd(sequence.getBodyBlock());
          calyx::EnableOp::create(builder, enable.getLoc(), name);
          enable->moveBefore(sequence.getBodyBlock(), sequence.getBodyBlock()->end());
        }
        if (transaction.cell != survivor) {
          for (auto [oldPort, newPort] : llvm::zip(transaction.cell->getResults(), survivor->getResults()))
            oldPort.replaceAllUsesWith(newPort);
          transaction.cell->erase();
        }
      }
      survivor->setAttr("llm2fpga.divider_shared", UnitAttr::get(getOperation().getContext()));
    }
  }
};
}

void registerTransactionalDividersPass() { PassRegistration<TransactionalDividers>(); }
