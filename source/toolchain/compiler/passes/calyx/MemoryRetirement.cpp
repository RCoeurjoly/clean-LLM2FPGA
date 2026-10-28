// Drain prior completions before a dynamic sequential-memory request group.
#include "circt/Dialect/Calyx/CalyxOps.h"
#include "circt/Dialect/Comb/CombOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/Pass/Pass.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringSet.h"
#include <map>
using namespace mlir;
using namespace circt;
namespace {
struct MemoryRetirement : PassWrapper<MemoryRetirement, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(MemoryRetirement)
  StringRef getArgument() const final { return "llm2fpga-retire-memory-requests"; }
  StringRef getDescription() const final {
    return "Wait for previous sequential-memory completions before requests";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<calyx::CalyxDialect, comb::CombDialect, hw::HWDialect>();
  }
  void runOnOperation() override {
    bool alreadyBound = false;
    getOperation().walk([&](calyx::PrimitiveOp primitive) {
      if (primitive->hasAttr("llm2fpga.binding")) {
        primitive.emitError("retirement must precede memory binding");
        alreadyBound = true;
      }
    });
    if (alreadyBound) { signalPassFailure(); return; }
    for (auto component : getOperation().getOps<calyx::ComponentOp>()) {
      std::map<std::string, std::string> retirementGroups;
      SmallVector<calyx::GroupOp> groups;
      llvm::StringSet<> groupNames;
      component.walk([&](calyx::GroupOp group) {
        groups.push_back(group);
        groupNames.insert(group.getSymName());
      });
      for (auto group : groups) {
        if (group->hasAttr("llm2fpga.retirement")) continue;
        SmallVector<Value> completions;
        for (auto assignment : group->getRegion(0).getOps<calyx::AssignOp>()) {
          auto memory = assignment.getDest().getDefiningOp<calyx::SeqMemoryOp>();
          if (memory && assignment.getDest() == memory.contentEn() &&
              !llvm::is_contained(completions, memory.done()))
            completions.push_back(memory.done());
        }
        if (completions.empty()) continue;
        std::string name = group.getSymName().str() + "_retire";
        if (!groupNames.insert(name).second) {
          group.emitError("memory retirement group name collision");
          signalPassFailure(); return;
        }
        OpBuilder builder(group->getParentOp());
        auto one = hw::ConstantOp::create(builder, group.getLoc(), builder.getI1Type(), 1);
        builder.setInsertionPoint(group);
        auto retirement = calyx::GroupOp::create(builder, group.getLoc(), name);
        builder.setInsertionPointToStart(&retirement->getRegion(0).front());
        SmallVector<Value> retired;
        for (Value done : completions)
          retired.push_back(comb::XorOp::create(builder, group.getLoc(), ValueRange{done, one}, false));
        Value ready = retired.size() == 1 ? retired.front() :
            comb::AndOp::create(builder, group.getLoc(), ValueRange(retired), false).getResult();
        calyx::GroupDoneOp::create(builder, group.getLoc(), one, ready);
        group->setAttr("llm2fpga.retirement", builder.getStringAttr(name));
        retirementGroups.emplace(group.getSymName().str(), name);
      }
      SmallVector<calyx::EnableOp> enables;
      component.walk([&](calyx::EnableOp enable) { enables.push_back(enable); });
      for (auto enable : enables) {
        auto found = retirementGroups.find(enable.getGroupNameAttr().getValue().str());
        if (found == retirementGroups.end()) continue;
        OpBuilder builder(enable);
        auto sequence = calyx::SeqOp::create(builder, enable.getLoc());
        builder.setInsertionPointToEnd(sequence.getBodyBlock());
        calyx::EnableOp::create(builder, enable.getLoc(), found->second);
        enable->moveBefore(sequence.getBodyBlock(), sequence.getBodyBlock()->end());
      }
    }
  }
};
}
void registerMemoryRetirementPass() { PassRegistration<MemoryRetirement>(); }
