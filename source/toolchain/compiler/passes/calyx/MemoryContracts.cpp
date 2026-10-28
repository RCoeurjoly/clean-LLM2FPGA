// Complete only the inert write-data port of proven read-only transactions.
// Dynamic or enabled writes without data are compiler errors, never guessed.
#include "circt/Dialect/Calyx/CalyxOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/SmallVector.h"

using namespace mlir;
using namespace circt;

namespace {
struct MemoryPorts { Value enable, data; };

MemoryPorts ports(Operation *operation) {
  if (!operation) return {};
  if (auto memory = dyn_cast<calyx::MemoryOp>(operation))
    return {memory.writeEn(), memory.writeData()};
  if (auto memory = dyn_cast<calyx::SeqMemoryOp>(operation))
    return {memory.writeEn(), memory.writeData()};
  return {};
}

struct MemoryContracts : PassWrapper<MemoryContracts, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(MemoryContracts)
  StringRef getArgument() const final {
    return "llm2fpga-complete-calyx-memory-contracts";
  }
  StringRef getDescription() const final {
    return "Prove omitted Calyx memory write data is inert, then bind zero";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<calyx::CalyxDialect, hw::HWDialect>();
  }
  void runOnOperation() override {
    SmallVector<calyx::AssignOp> incomplete;
    bool invalid = false;
    getOperation().walk([&](calyx::AssignOp assignment) {
      auto memory = ports(assignment.getDest().getDefiningOp());
      if (!memory.enable || assignment.getDest() != memory.enable)
        return;
      auto group = dyn_cast<calyx::GroupOp>(assignment->getParentOp());
      if (!group) {
        assignment.emitError("memory write_en outside a supported dynamic group");
        invalid = true;
        return;
      }
      unsigned enableDrivers = 0, dataDrivers = 0;
      for (auto sibling : group->getRegion(0).getOps<calyx::AssignOp>()) {
        enableDrivers += sibling.getDest() == memory.enable;
        dataDrivers += sibling.getDest() == memory.data;
      }
      if (dataDrivers) return;
      auto constant = assignment.getSrc().getDefiningOp<hw::ConstantOp>();
      if (enableDrivers != 1 || assignment.getGuard() || !constant ||
          !constant.getValue().isZero()) {
        assignment.emitError("missing write_data requires a unique unconditional zero write_en");
        invalid = true;
        return;
      }
      incomplete.push_back(assignment);
    });
    if (invalid) { signalPassFailure(); return; }
    for (auto assignment : incomplete) {
      auto memory = ports(assignment.getDest().getDefiningOp());
      auto component = assignment->getParentOfType<calyx::ComponentOp>();
      OpBuilder builder(&component->getRegion(0).front(),
                        component->getRegion(0).front().begin());
      auto zero = hw::ConstantOp::create(builder, assignment.getLoc(),
                                         memory.data.getType(), 0);
      builder.setInsertionPoint(assignment);
      calyx::AssignOp::create(builder, assignment.getLoc(), memory.data, zero);
    }
  }
};
} // namespace

void registerMemoryContractPass() { PassRegistration<MemoryContracts>(); }
