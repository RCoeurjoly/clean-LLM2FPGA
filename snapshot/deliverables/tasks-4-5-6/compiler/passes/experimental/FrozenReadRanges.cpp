// Experimental typed load narrowing. No model names, sampled bounds or RTL edits.
#include "mlir/Dialect/Arith/IR/Arith.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Dialect/MemRef/IR/MemRef.h"
#include "mlir/Analysis/AliasAnalysis.h"
#include "mlir/Interfaces/SideEffectInterfaces.h"
#include "mlir/IR/Builders.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Tools/Plugins/PassPlugin.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/SHA256.h"
#include <map>
using namespace mlir;
namespace {
static std::string sha(StringRef bytes) {
  auto hash = llvm::SHA256::hash(llvm::ArrayRef<uint8_t>(
      reinterpret_cast<const uint8_t *>(bytes.data()), bytes.size()));
  return llvm::toHex(llvm::ArrayRef<uint8_t>(hash), true);
}
struct Range {
  std::string tensorHash;
  unsigned storage, bits;
  uint64_t words;
  bool signedExtension;
};

// Follow only explicit, known aliases/calls. Unknown effects or escapes reject.
static bool readonly(Value value, ModuleOp module) {
  AliasAnalysis aliases(module);
  SmallVector<Value> work{value};
  llvm::DenseSet<Value> visited;
  while (!work.empty()) {
    Value current = work.pop_back_val();
    if (!visited.insert(current).second) continue;
    Operation *owner = current.getParentRegion()->getParentOp();
    auto function = dyn_cast<func::FuncOp>(owner);
    if (!function) function = owner->getParentOfType<func::FuncOp>();
    if (!function) return false;
    bool safe = true;
    function.walk([&](Operation *operation) {
      if (auto effects = dyn_cast<MemoryEffectOpInterface>(operation)) {
        SmallVector<MemoryEffects::EffectInstance> instances;
        effects.getEffects(instances);
        for (auto effect : instances)
          if (isa<MemoryEffects::Write>(effect.getEffect()) &&
              (!effect.getValue() || !aliases.alias(current, effect.getValue()).isNo()))
            safe = false;
      } else if (auto call = dyn_cast<func::CallOp>(operation)) {
        auto callee = module.lookupSymbol<func::FuncOp>(call.getCallee());
        if (!callee || callee.isExternal()) { safe = false; return; }
        // Conservative cross-function check: no inferred disjointness for
        // callee parameters, globals, nested calls or opaque effects.
        callee.walk([&](Operation *nested) {
          if (isa<func::CallOp>(nested)) { safe = false; return; }
          if (auto effects = dyn_cast<MemoryEffectOpInterface>(nested)) {
            SmallVector<MemoryEffects::EffectInstance> instances;
            effects.getEffects(instances);
            for (auto effect : instances)
              if (isa<MemoryEffects::Write>(effect.getEffect()) &&
                  (!effect.getValue() || !aliases.alias(current, effect.getValue()).isNo())) safe = false;
          } else if (nested->getNumRegions() == 0 && !isMemoryEffectFree(nested)) safe = false;
        });
        for (auto operand : llvm::enumerate(call.getOperands())) {
          if (!isa<BaseMemRefType>(operand.value().getType()) ||
              aliases.alias(current, operand.value()).isNo()) continue;
          if (!callee || callee.isExternal()) safe = false;
          else work.push_back(callee.getArgument(operand.index()));
        }
      } else if (operation->getNumRegions() == 0 && !isMemoryEffectFree(operation)) {
        safe = false;
      }
    });
    if (!safe) return false;
    for (Operation *user : current.getUsers()) {
      if (auto load = dyn_cast<memref::LoadOp>(user)) {
        if (load.getMemRef() == current) continue;
      }
      if (isa<memref::DimOp>(user)) continue;
      if (isa<memref::CastOp, memref::SubViewOp, memref::ReinterpretCastOp>(user)) {
        work.append(user->result_begin(), user->result_end());
        continue;
      }
      if (auto copy = dyn_cast<memref::CopyOp>(user)) {
        if (copy.getSource() == current && copy.getTarget() != current) continue;
      }
      if (auto call = dyn_cast<func::CallOp>(user)) {
        auto callee = module.lookupSymbol<func::FuncOp>(call.getCallee());
        if (!callee || callee.isExternal() ||
            llvm::any_of(call.getResultTypes(), [](Type t) { return isa<BaseMemRefType>(t); }))
          return false;
        for (auto operand : llvm::enumerate(call.getOperands()))
          if (operand.value() == current) work.push_back(callee.getArgument(operand.index()));
        continue;
      }
      return false;
    }
  }
  return true;
}

struct FrozenReads : PassWrapper<FrozenReads, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(FrozenReads)
  FrozenReads() = default;
  FrozenReads(const FrozenReads &other) : PassWrapper(other) {}
  Option<std::string> certificate{*this, "certificate"};
  Option<std::string> certificateHash{*this, "certificate-sha256"};
  StringRef getArgument() const final { return "llm2fpga-narrow-frozen-reads"; }
  StringRef getDescription() const final { return "Narrow authenticated frozen loads after readonly alias proof"; }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<arith::ArithDialect, memref::MemRefDialect, func::FuncDialect>();
  }
  void runOnOperation() override {
    auto module = getOperation();
    auto fail = [&](StringRef message) {
      module.emitError() << "frozen read range: " << message;
      signalPassFailure();
    };
    auto file = llvm::MemoryBuffer::getFile(certificate);
    if (!file || sha((*file)->getBuffer()) != certificateHash) {
      fail("certificate identity mismatch"); return;
    }
    auto parsed = llvm::json::parse((*file)->getBuffer());
    if (!parsed) { fail("invalid certificate JSON"); return; }
    auto *object = parsed->getAsObject();
    if (!object || object->getInteger("schema_version") != 1 || !object->getArray("tensors")) {
      fail("invalid certificate schema"); return;
    }
    std::map<std::string, Range> ranges;
    for (auto &row : *object->getArray("tensors")) {
      auto *entry = row.getAsObject();
      if (!entry) { fail("invalid tensor record"); return; }
      auto target = entry->getString("target"), tensor = entry->getString("tensor_sha256");
      auto image = entry->getString("image"), imageHash = entry->getString("image_sha256");
      auto extension = entry->getString("extension");
      auto storage = entry->getInteger("storage_width"), bits = entry->getInteger("value_width");
      auto words = entry->getInteger("words"), low = entry->getInteger("minimum"), high = entry->getInteger("maximum");
      if (!target || target->empty() || !tensor || tensor->size() != 64 || !image ||
          !image->starts_with("/") || !imageHash || !extension || !storage ||
          (*storage != 32 && *storage != 64) || !bits ||
          (*bits != 8 && *bits != 16 && *bits != 32 && *bits != 64) || *bits > *storage ||
          !words || *words <= 0 || !low || !high ||
          (*extension != "signed" && *extension != "unsigned") ||
          ranges.count(target->str())) { fail("invalid tensor geometry/range"); return; }
      auto data = llvm::MemoryBuffer::getFile(*image);
      if (!data || sha((*data)->getBuffer()) != *imageHash) {
        fail("image identity mismatch"); return;
      }
      SmallVector<StringRef> lines;
      (*data)->getBuffer().split(lines, '\n', -1, false);
      if (lines.size() != static_cast<uint64_t>(*words)) { fail("image word count mismatch"); return; }
      std::string raw;
      int64_t minimum = INT64_MAX, maximum = INT64_MIN;
      for (StringRef line : lines) {
        line = line.trim();
        if (line.size() != static_cast<size_t>(*storage / 4) ||
            !llvm::all_of(line, [](char c) { return llvm::isHexDigit(c); })) {
          fail("invalid image word"); return;
        }
        llvm::APInt value(*storage, line, 16);
        auto restored = *extension == "signed" ? value.trunc(*bits).sext(*storage)
                                               : value.trunc(*bits).zext(*storage);
        if (value != restored) { fail("image violates declared range"); return; }
        minimum = std::min(minimum, value.getSExtValue());
        maximum = std::max(maximum, value.getSExtValue());
        auto unsignedValue = value.getZExtValue();
        for (unsigned i = 0; i < *storage / 8; ++i) raw.push_back((unsignedValue >> (8*i)) & 255);
      }
      if (sha(raw) != *tensor || minimum != *low || maximum != *high ||
          (minimum < 0) != (*extension == "signed")) {
        fail("tensor identity/bounds mismatch"); return;
      }
      ranges.emplace(target->str(), Range{tensor->str(), static_cast<unsigned>(*storage),
          static_cast<unsigned>(*bits), static_cast<uint64_t>(*words), *extension == "signed"});
    }
    struct Pending { memref::LoadOp load; Range range; };
    SmallVector<Pending> pending;
    for (auto function : module.getOps<func::FuncOp>()) {
      if (function.isExternal()) continue;
      for (auto argument : function.getArguments()) {
        auto origin = function.getArgAttrOfType<DictionaryAttr>(argument.getArgNumber(), "llm2fpga.input_origin");
        if (!origin) continue;
        auto kind = origin.getAs<StringAttr>("kind");
        if (!kind || kind.getValue() == "USER_INPUT") continue;
        auto target = origin.getAs<StringAttr>("target"), identity = origin.getAs<StringAttr>("sha256");
        auto found = target ? ranges.find(target.getValue().str()) : ranges.end();
        if (found == ranges.end() || !identity || identity.getValue() != found->second.tensorHash) {
          fail("argument origin does not match certificate"); return;
        }
        auto range = found->second;
        auto type = dyn_cast<MemRefType>(argument.getType());
        uint64_t elements = 1;
        bool geometry = type && type.hasStaticShape() && type.getElementType().isInteger(range.storage);
        if (geometry) for (int64_t dimension : type.getShape()) {
          if (dimension <= 0 || elements > range.words / static_cast<uint64_t>(dimension)) { geometry = false; break; }
          elements *= dimension;
        }
        if (!geometry || elements != range.words) {
          fail("argument storage geometry mismatch"); return;
        }
        if (range.bits == range.storage) continue;
        if (!readonly(argument, module)) { fail("frozen argument is not provably readonly"); return; }
        for (auto *user : argument.getUsers())
          if (auto load = dyn_cast<memref::LoadOp>(user)) pending.push_back({load, range});
      }
    }
    // All metadata and aliases have passed before the first rewrite.
    for (auto item : pending) {
      OpBuilder builder(item.load);
      builder.setInsertionPointAfter(item.load);
      auto narrow = arith::TruncIOp::create(builder, item.load.getLoc(),
          builder.getIntegerType(item.range.bits), item.load.getResult());
      Value wide = item.range.signedExtension
          ? arith::ExtSIOp::create(builder, item.load.getLoc(), item.load.getType(), narrow).getResult()
          : arith::ExtUIOp::create(builder, item.load.getLoc(), item.load.getType(), narrow).getResult();
      item.load.getResult().replaceAllUsesExcept(wide, narrow.getOperation());
    }
    module->setAttr("llm2fpga.narrowed_frozen_reads", IntegerAttr::get(IntegerType::get(&getContext(), 64), pending.size()));
  }
};
}
extern "C" mlir::PassPluginLibraryInfo mlirGetPassPluginInfo() {
  return {MLIR_PLUGIN_API_VERSION, "llm2fpga-frozen-read-ranges", "0.1",
          []() { PassRegistration<FrozenReads>(); }};
}
