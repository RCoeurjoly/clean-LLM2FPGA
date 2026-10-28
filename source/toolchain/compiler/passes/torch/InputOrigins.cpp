// Bind exported graph-input metadata to typed function arguments before lowering.
#include "TypedTensor.h"
#include "mlir/Dialect/Func/IR/FuncOps.h"
#include "mlir/Pass/Pass.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/SHA256.h"
#include "llvm/ADT/StringExtras.h"
#include <set>
using namespace mlir;
namespace {
struct InputOrigins : PassWrapper<InputOrigins, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(InputOrigins)
  InputOrigins() = default;
  InputOrigins(const InputOrigins &other) : PassWrapper(other) {}
  Option<std::string> manifest{*this, "manifest"};
  Option<std::string> manifestHash{*this, "manifest-sha256"};
  StringRef getArgument() const final { return "llm2fpga-annotate-graph-inputs"; }
  StringRef getDescription() const final { return "Authenticate graph input origins on typed arguments"; }
  void runOnOperation() override {
    auto fail = [&](StringRef message) {
      getOperation().emitError() << "graph input origin: " << message;
      signalPassFailure();
    };
    auto file = llvm::MemoryBuffer::getFile(manifest);
    if (!file) { fail("missing manifest"); return; }
    auto bytes = (*file)->getBuffer();
    auto hash = llvm::SHA256::hash(llvm::ArrayRef<uint8_t>(
      reinterpret_cast<const uint8_t *>(bytes.data()), bytes.size()));
    if (llvm::toHex(llvm::ArrayRef<uint8_t>(hash), true) != manifestHash) {
      fail("manifest identity mismatch"); return;
    }
    auto parsed = llvm::json::parse(bytes);
    if (!parsed) { fail(llvm::toString(parsed.takeError())); return; }
    auto *object = parsed->getAsObject();
    auto function = getOperation().lookupSymbol<func::FuncOp>("main");
    if (!object || object->getInteger("schema_version") != 1 ||
        !object->getString("signature_sha256") || !object->getArray("inputs") ||
        !function || function.isDeclaration() ||
        object->getArray("inputs")->size() != function.getNumArguments()) {
      fail("manifest/function signature mismatch"); return;
    }
    OpBuilder builder(&getContext());
    SmallVector<DictionaryAttr> origins;
    std::set<std::string> names;
    for (auto indexed : llvm::enumerate(*object->getArray("inputs"))) {
      auto *input = indexed.value().getAsObject();
      if (!input) { fail("invalid input record"); return; }
      auto index = input->getInteger("index");
      auto name = input->getString("name"), kind = input->getString("kind");
      auto dtype = input->getString("dtype");
      auto *shape = input->getArray("shape");
      if (!index || *index != static_cast<int64_t>(indexed.index()) || !name ||
          !names.insert(name->str()).second || !kind || !dtype || !shape ||
          !(*kind == "BUFFER" || *kind == "PARAMETER" || *kind == "CONSTANT_TENSOR" ||
            *kind == "USER_INPUT")) { fail("invalid ordered input record"); return; }
      auto type = wrap(function.getArgument(indexed.index()).getType());
      if (!torchMlirTypeIsATorchValueTensor(type) ||
          !torchMlirTorchValueTensorTypeHasSizes(type) ||
          !torchMlirTorchValueTensorTypeHasDtype(type)) {
        fail("argument geometry is not a typed static tensor"); return;
      }
      auto integer = dyn_cast<IntegerType>(unwrap(torchMlirTorchValueTensorTypeGetDtype(type)));
      int64_t rank = torchMlirTorchValueTensorTypeGetRank(type);
      if (!integer || !integer.isSigned() || rank < 0 ||
          static_cast<size_t>(rank) != shape->size() ||
          !((*dtype == "torch.int64" && integer.getWidth() == 64) ||
            (*dtype == "torch.int32" && integer.getWidth() == 32))) {
        fail("argument geometry/dtype mismatch"); return;
      }
      SmallVector<int64_t> dimensions(rank);
      torchMlirTorchValueTensorTypeGetSizes(type, dimensions.data());
      for (auto dimension : llvm::enumerate(*shape)) {
        auto value = dimension.value().getAsInteger();
        if (!value || *value != dimensions[dimension.index()] || *value <= 0) {
          fail("argument geometry mismatch"); return;
        }
      }
      NamedAttrList attributes;
      attributes.set("index", builder.getI64IntegerAttr(*index));
      attributes.set("name", builder.getStringAttr(*name));
      attributes.set("kind", builder.getStringAttr(*kind));
      auto target = input->getString("target"), identity = input->getString("sha256");
      if (*kind != "USER_INPUT") {
        if (!target || !identity || identity->size() != 64 ||
            !llvm::all_of(*identity, [](char c) { return llvm::isHexDigit(c); })) {
          fail("frozen input identity required"); return;
        }
        attributes.set("target", builder.getStringAttr(*target));
        attributes.set("sha256", builder.getStringAttr(*identity));
      } else {
        auto *identityField = input->get("sha256");
        if (!identityField || identityField->kind() != llvm::json::Value::Null) {
          fail("runtime input cannot contain frozen values"); return;
        }
      }
      origins.push_back(attributes.getDictionary(&getContext()));
    }
    // Authenticate every argument before annotating any of them.
    for (auto indexed : llvm::enumerate(origins))
      function.setArgAttr(indexed.index(), "llm2fpga.input_origin", indexed.value());
    function->setAttr("llm2fpga.input_manifest_sha256", builder.getStringAttr(manifestHash));
    function->setAttr("llm2fpga.graph_signature_sha256",
        builder.getStringAttr(*object->getString("signature_sha256")));
  }
};
}
void llm2fpga::registerInputOrigins() { PassRegistration<InputOrigins>(); }
