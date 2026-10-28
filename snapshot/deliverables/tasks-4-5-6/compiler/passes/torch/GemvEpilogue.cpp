// Fuse a sole tensor epilogue after a completed wrapping serial dot product.
#include "TypedTensor.h"
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Linalg/IR/Linalg.h"
#include "mlir/Dialect/SCF/IR/SCF.h"
#include "mlir/Dialect/Tensor/IR/Tensor.h"
#include "mlir/IR/IRMapping.h"
#include "mlir/IR/Matchers.h"
#include "mlir/Interfaces/SideEffectInterfaces.h"
#include "mlir/Pass/Pass.h"
using namespace mlir;
namespace {
bool identityOnDomain(AffineMap map, ArrayRef<int64_t> shape) {
  if (map.getNumSymbols() || map.getNumDims()!=shape.size() ||
      map.getNumResults()!=shape.size()) return false;
  for (auto [i,e] : llvm::enumerate(map.getResults())) {
    if (auto d=dyn_cast<AffineDimExpr>(e)) { if (d.getPosition()!=i) return false; }
    else if (auto c=dyn_cast<AffineConstantExpr>(e)) {
      if (c.getValue()!=0 || shape[i]!=1) return false;
    } else return false;
  }
  return true;
}
bool fuse(linalg::GenericOp consumer) {
  if (!consumer.hasPureTensorSemantics() || consumer.getNumResults()!=1 ||
      consumer.getOutputs().size()!=1 ||
      !consumer->getDiscardableAttrDictionary().empty() ||
      llvm::any_of(consumer.getIteratorTypesArray(), [](auto t) {
        return t!=utils::IteratorType::parallel;
      })) return false;
  auto type=dyn_cast<RankedTensorType>(consumer.getResult(0).getType());
  if (!type || !type.hasStaticShape() || type.getRank()!=2 ||
      !type.getElementType().isInteger(64) ||
      llvm::any_of(type.getShape(),[](int64_t n){return n<=0;})) return false;
  auto ranges=consumer.getStaticLoopRanges();
  auto maps=consumer.getIndexingMapsArray();
  if (!llvm::equal(ranges,type.getShape()) ||
      !identityOnDomain(maps.back(),type.getShape()) ||
      !consumer.getBody()->getArguments().back().use_empty()) return false;
  for (Operation &op:consumer.getBody()->without_terminator()) {
    if (op.getNumRegions() || !isMemoryEffectFree(&op)) return false;
    for (Value v:op.getOperands())
      if (!isa<IntegerType,IndexType>(v.getType())) return false;
    if (auto index=dyn_cast<linalg::IndexOp>(op))
      if (index.getDim()>1) return false;
  }
  for (unsigned input=0;input<consumer.getInputs().size();++input) {
    Value value=consumer.getInputs()[input];
    auto producer=value.getDefiningOp<linalg::GenericOp>();
    if (!producer || !producer.hasPureTensorSemantics() || !value.hasOneUse() ||
        producer->getBlock()!=consumer->getBlock() || producer.getNumResults()!=1 ||
        producer.getInputs().size()!=2 || producer.getOutputs().size()!=1 ||
        producer.getResult(0).getType()!=type ||
        !producer->getDiscardableAttrDictionary().empty() ||
        !identityOnDomain(maps[input],type.getShape())) continue;
    auto iters=producer.getIteratorTypesArray();
    if (iters!=SmallVector<utils::IteratorType>{utils::IteratorType::parallel,
          utils::IteratorType::parallel,utils::IteratorType::reduction}) continue;
    auto left=dyn_cast<RankedTensorType>(producer.getInputs()[0].getType());
    auto right=dyn_cast<RankedTensorType>(producer.getInputs()[1].getType());
    if (!left || !right || !left.hasStaticShape() || !right.hasStaticShape() ||
        left.getRank()!=2 || right.getRank()!=2 ||
        left.getElementType()!=type.getElementType() || right.getElementType()!=type.getElementType() ||
        left.getDimSize(0)!=type.getDimSize(0) || right.getDimSize(0)!=type.getDimSize(1) ||
        left.getDimSize(1)<=0 || left.getDimSize(1)!=right.getDimSize(1)) continue;
    AffineExpr r,o,k;bindDims(consumer.getContext(),r,o,k);
    SmallVector<AffineMap> expected{
      AffineMap::get(3,0,{r,k},consumer.getContext()),
      AffineMap::get(3,0,{o,k},consumer.getContext()),
      AffineMap::get(3,0,{r,o},consumer.getContext())};
    if (producer.getIndexingMapsArray()!=expected) continue;
    auto fill=producer.getOutputs()[0].getDefiningOp<linalg::FillOp>();
    APInt number;
    if (!fill || fill.getInputs().size()!=1 ||
        !matchPattern(fill.getInputs()[0],m_ConstantInt(&number)) || !number.isZero()) continue;
    auto &body=*producer.getBody();
    if (body.getOperations().size()!=3) continue;
    auto mul=dyn_cast<arith::MulIOp>(body.front());
    auto add=dyn_cast<arith::AddIOp>(*std::next(body.begin()));
    auto yield=dyn_cast<linalg::YieldOp>(body.getTerminator());
    if (!mul || !add || !yield || yield.getNumOperands()!=1 ||
        mul.getOverflowFlags()!=arith::IntegerOverflowFlags::none ||
        add.getOverflowFlags()!=arith::IntegerOverflowFlags::none ||
        !((mul.getLhs()==body.getArgument(0) && mul.getRhs()==body.getArgument(1)) ||
          (mul.getRhs()==body.getArgument(0) && mul.getLhs()==body.getArgument(1))) ||
        !((add.getLhs()==body.getArgument(2) && add.getRhs()==mul.getResult()) ||
          (add.getRhs()==body.getArgument(2) && add.getLhs()==mul.getResult())) ||
        yield.getOperand(0)!=add.getResult()) continue;
    OpBuilder builder(consumer);
    SmallVector<Value> inputs(consumer.getInputs());inputs.erase(inputs.begin()+input);
    auto newMaps=maps;newMaps.erase(newMaps.begin()+input);
    Value lhs=producer.getInputs()[0],rhs=producer.getInputs()[1],zero=fill.getInputs()[0];
    auto fused=linalg::GenericOp::create(builder,consumer.getLoc(),TypeRange{type},
      inputs,consumer.getOutputs(),newMaps,consumer.getIteratorTypesArray(),
      [&](OpBuilder &nested,Location loc,ValueRange args) {
        Value row=linalg::IndexOp::create(nested,loc,0);
        Value col=linalg::IndexOp::create(nested,loc,1);
        Value lo=arith::ConstantIndexOp::create(nested,loc,0);
        Value hi=arith::ConstantIndexOp::create(nested,loc,left.getDimSize(1));
        Value step=arith::ConstantIndexOp::create(nested,loc,1);
        auto dot=scf::ForOp::create(nested,loc,lo,hi,step,ValueRange{zero},
          [&](OpBuilder &reduce,Location at,Value iv,ValueRange acc) {
            Value a=tensor::ExtractOp::create(reduce,at,lhs,ValueRange{row,iv});
            Value b=tensor::ExtractOp::create(reduce,at,rhs,ValueRange{col,iv});
            Value product=arith::MulIOp::create(reduce,at,a,b);
            Value sum=arith::AddIOp::create(reduce,at,acc[0],product);
            scf::YieldOp::create(reduce,at,ValueRange{sum});
          });
        IRMapping mapping;
        unsigned next=0;
        for (auto [i,arg]:llvm::enumerate(consumer.getBody()->getArguments()))
          mapping.map(arg,i==input?dot.getResult(0):args[next++]);
        for (Operation &op:consumer.getBody()->without_terminator()) {
          if (auto index=dyn_cast<linalg::IndexOp>(op))
            mapping.map(index.getResult(),index.getDim()==0?row:col);
          else nested.clone(op,mapping);
        }
        auto term=cast<linalg::YieldOp>(consumer.getBody()->getTerminator());
        linalg::YieldOp::create(nested,loc,mapping.lookupOrDefault(term.getOperand(0)));
      });
    fused->setAttr("llm2fpga.gemv_epilogue",builder.getUnitAttr());
    consumer.getResult(0).replaceAllUsesWith(fused.getResult(0));consumer.erase();
    producer.erase();return true;
  }
  return false;
}
struct GemvEpilogue:PassWrapper<GemvEpilogue,OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(GemvEpilogue)
  StringRef getArgument() const final{return "llm2fpga-fuse-gemv-epilogues";}
  StringRef getDescription() const final{return "Keep a wrapping serial dot product scalar through its sole tensor epilogue";}
  void getDependentDialects(DialectRegistry &r) const override {
    r.insert<arith::ArithDialect,linalg::LinalgDialect,scf::SCFDialect,tensor::TensorDialect>();
  }
  void runOnOperation() override {
    SmallVector<linalg::GenericOp> candidates;
    getOperation().walk([&](linalg::GenericOp op){candidates.push_back(op);});
    for(auto op:candidates) fuse(op);
  }
};
}
void llm2fpga::registerGemvEpilogue(){PassRegistration<GemvEpilogue>();}
