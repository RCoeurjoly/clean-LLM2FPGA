// Authenticate a complete memory plan before subsequent cell replacement.
// These attributes are compiler inputs, never expected outputs.
#include "circt/Dialect/Calyx/CalyxOps.h"
#include "circt/Dialect/HW/HWOps.h"
#include "mlir/IR/Builders.h"
#include "mlir/Pass/Pass.h"
#include "mlir/Pass/PassManager.h"
#include "llvm/ADT/APInt.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/SHA256.h"
#include <map>
#include <set>

using namespace mlir;
using namespace circt;

namespace {
using Key = std::pair<std::string, std::string>;

std::string sha256(StringRef bytes) {
  auto hash = llvm::SHA256::hash(llvm::ArrayRef<uint8_t>(
      reinterpret_cast<const uint8_t *>(bytes.data()), bytes.size()));
  return llvm::toHex(llvm::ArrayRef<uint8_t>(hash), true);
}

struct MemoryBindings
    : PassWrapper<MemoryBindings, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(MemoryBindings)
  MemoryBindings() = default;
  MemoryBindings(const MemoryBindings &other) : PassWrapper(other) {}
  Option<std::string> manifest{*this, "manifest",
      llvm::cl::desc("Memory binding manifest filename")};
  Option<std::string> manifestHash{*this, "manifest-sha256",
      llvm::cl::desc("Expected SHA256 of the manifest bytes")};

  StringRef getArgument() const final {
    return "llm2fpga-validate-memory-bindings";
  }
  StringRef getDescription() const final {
    return "Validate typed memory geometry, mutability, latency and initializers";
  }
  void runOnOperation() override {
    auto fail = [&](StringRef message) {
      getOperation().emitError() << "memory binding: " << message;
      signalPassFailure();
    };
    auto file = llvm::MemoryBuffer::getFile(manifest);
    if (!file) { fail("missing manifest"); return; }
    if (sha256((*file)->getBuffer()) != manifestHash) {
      fail("manifest identity mismatch"); return;
    }
    auto parsed = llvm::json::parse((*file)->getBuffer());
    if (!parsed) {
      fail(llvm::toString(parsed.takeError())); return;
    }
    auto *object = parsed->getAsObject();
    if (!object || object->getInteger("schema_version") != 1 ||
        !object->getArray("bindings")) {
      fail("invalid manifest schema"); return;
    }
    std::map<Key, calyx::SeqMemoryOp> cells;
    bool unsupported = false;
    getOperation().walk([&](calyx::MemoryOp memory) {
      memory.emitError("memory binding requires sequential memory IR");
      unsupported = true;
    });
    getOperation().walk([&](calyx::SeqMemoryOp memory) {
      auto component = memory->getParentOfType<calyx::ComponentOp>();
      cells.emplace(Key{component.getName().str(), memory.instanceName().str()}, memory);
    });
    if (unsupported) { signalPassFailure(); return; }
    std::set<Key> seen;
    SmallVector<std::pair<calyx::SeqMemoryOp, DictionaryAttr>> validated;
    OpBuilder builder(&getContext());
    for (auto &value : *object->getArray("bindings")) {
      auto *binding = value.getAsObject();
      if (!binding) { fail("invalid binding object"); return; }
      auto component = binding->getString("component"), cell = binding->getString("cell");
      auto width = binding->getInteger("width"), depth = binding->getInteger("depth");
      auto address = binding->getInteger("address_width");
      auto kind = binding->getString("implementation");
      auto latency = binding->getInteger("read_latency");
      auto writable = binding->getBoolean("writable");
      if (!component || !cell || !width || !depth || !address || !kind ||
          !latency || !writable) {
        fail("missing or invalid binding field"); return;
      }
      auto storageWidth = binding->getInteger("storage_width");
      auto signExtend = binding->getBoolean("sign_extend");
      bool narrow = binding->get("storage_width") || binding->get("sign_extend");
      if (narrow && (!storageWidth || !signExtend || *storageWidth <= 0 ||
                     *storageWidth > *width)) {
        fail("invalid storage narrowing fields"); return;
      }
      if (narrow && *writable) {
        fail("storage narrowing requires immutable memory"); return;
      }
      auto valueWidth = binding->getInteger("value_width");
      auto valueSignExtend = binding->getBoolean("value_sign_extend");
      bool boundRead = binding->get("value_width") || binding->get("value_sign_extend");
      if (boundRead && (!valueWidth || !valueSignExtend || *valueWidth <= 0 || *valueWidth > *width)) {
        fail("invalid read value bounds"); return;
      }
      if (boundRead && *writable) {
        fail("read value bounds require immutable memory"); return;
      }
      Key key{component->str(), cell->str()};
      auto found = cells.find(key);
      if (found == cells.end()) { fail("binding names an absent memory cell"); return; }
      if (!seen.insert(key).second) { fail("duplicate memory binding"); return; }
      auto memory = found->second;
      if (*width <= 0 || *width > 4096 || *depth <= 0 || *address <= 0 ||
          *address > 63 || memory.getSizes().size() != 1 ||
          memory.getAddrSizes().size() != 1 || memory.getWidth() != *width ||
          cast<IntegerAttr>(memory.getSizes()[0]).getInt() != *depth ||
          cast<IntegerAttr>(memory.getAddrSizes()[0]).getInt() != *address ||
          static_cast<uint64_t>(*depth) > (uint64_t(1) << *address)) {
        fail("cell geometry mismatch"); return;
      }
      if (!((*kind == "register" && *latency == 1) ||
            ((*kind == "bram" || *kind == "lutram") && *latency == 2) ||
            (*kind == "ddr3" && *latency == 0))) {
        fail("implementation/latency mismatch"); return;
      }
      // Zero represents a completion-driven interface, never zero-cycle data.
      // Initially offload immutable full-width inputs only. Packed storage and
      // writes need independently qualified packing/ordering contracts.
      if (*kind == "ddr3" && (*writable || narrow)) {
        fail("off-chip binding requires immutable full-width storage"); return;
      }
      if (!*writable) {
        bool writes = false;
        for (auto &use : memory.writeEn().getUses()) {
          auto assignment = dyn_cast<calyx::AssignOp>(use.getOwner());
          if (!assignment || assignment.getDest() != memory.writeEn()) {
            writes = true; break;
          }
          auto constant = assignment.getSrc().getDefiningOp<hw::ConstantOp>();
          if (!constant || !constant.getValue().isZero()) { writes = true; break; }
        }
        if (writes) { fail("immutable memory write is not proven disabled"); return; }
      }
      NamedAttrList attributes;
      attributes.set("width", builder.getI64IntegerAttr(*width));
      attributes.set("depth", builder.getI64IntegerAttr(*depth));
      attributes.set("address_width", builder.getI64IntegerAttr(*address));
      attributes.set("implementation", builder.getStringAttr(*kind));
      attributes.set("read_latency", builder.getI64IntegerAttr(*latency));
      attributes.set("writable", builder.getBoolAttr(*writable));
      if (narrow) {
        attributes.set("storage_width", builder.getI64IntegerAttr(*storageWidth));
        attributes.set("sign_extend", builder.getBoolAttr(*signExtend));
      }
      if (boundRead) {
        attributes.set("value_width", builder.getI64IntegerAttr(*valueWidth));
        attributes.set("value_sign_extend", builder.getBoolAttr(*valueSignExtend));
      }
      auto image = binding->getString("init_file");
      auto imageHash = binding->getString("init_sha256");
      if (!image) {
        if (!*writable && memory->hasAttr("external")) {
          fail("immutable memory requires initializer"); return;
        }
        auto *imageField = binding->get("init_file");
        auto *hashField = binding->get("init_sha256");
        if (!imageField || !hashField || imageField->kind() != llvm::json::Value::Null ||
            hashField->kind() != llvm::json::Value::Null) {
          fail("missing or invalid initializer fields"); return;
        }
      } else {
        if (image->empty() || llvm::sys::path::filename(*image) != *image ||
            *image == "." || *image == ".." || image->contains('\\') ||
            !image->ends_with(".hex")) {
          fail("initializer filename must be a local hex file"); return;
        }
        llvm::SmallString<256> imagePath(manifest.getValue());
        llvm::sys::path::remove_filename(imagePath);
        llvm::sys::path::append(imagePath, *image);
        auto data = llvm::MemoryBuffer::getFile(imagePath);
        if (!data) { fail("missing initializer file"); return; }
        if (!imageHash || sha256((*data)->getBuffer()) != *imageHash) {
          fail("initializer identity mismatch"); return;
        }
        SmallVector<StringRef> rows;
        (*data)->getBuffer().split(rows, '\n', -1, true);
        if (!rows.empty() && rows.back().empty()) rows.pop_back();
        if (rows.size() != static_cast<uint64_t>(*depth)) {
          fail("initializer depth mismatch"); return;
        }
        for (StringRef row : rows) {
          if (row.ends_with("\r")) row = row.drop_back();
          if (row.size() != static_cast<uint64_t>((*width + 3) / 4) ||
              !llvm::all_of(row, [](char c) { return llvm::isHexDigit(c); }) ||
              (*width % 4 && llvm::hexDigitValue(row.front()) >= (1u << (*width % 4)))) {
            fail("initializer width mismatch"); return;
          }
          if (narrow) {
            llvm::APInt original(*width, row, 16);
            auto stored = original.trunc(*storageWidth);
            auto restored = *signExtend ? stored.sext(*width) : stored.zext(*width);
            if (restored != original) {
              fail("storage narrowing loses initializer bits"); return;
            }
          }
          if (boundRead) {
            llvm::APInt original(*width, row, 16);
            auto stored = original.trunc(*valueWidth);
            auto restored = *valueSignExtend ? stored.sext(*width) : stored.zext(*width);
            if (restored != original) {
              fail("read value bounds lose initializer bits"); return;
            }
          }
        }
        attributes.set("init_file", builder.getStringAttr(*image));
        attributes.set("init_sha256", builder.getStringAttr(*imageHash));
      }
      validated.emplace_back(memory, attributes.getDictionary(&getContext()));
    }
    if (seen.size() != cells.size()) { fail("missing memory binding"); return; }
    std::map<Key, DictionaryAttr> contracts;
    std::map<std::string, std::set<std::string>> requiredReferences;
    for (auto &[memory, attributes] : validated) {
      auto component = memory->getParentOfType<calyx::ComponentOp>();
      contracts.emplace(Key{component.getName().str(), memory.instanceName().str()}, attributes);
      if (!memory->hasAttr("external"))
        requiredReferences[component.getName().str()].insert(memory.instanceName().str());
    }
    bool invalidReference = false;
    getOperation().walk([&](calyx::InvokeOp invoke) {
      if (invalidReference) return;
      auto caller = invoke->getParentOfType<calyx::ComponentOp>();
      auto instance = caller.lookupSymbol<calyx::InstanceOp>(invoke.getCallee());
      if (!instance && invoke.getRefCellsMap().empty()) return;
      if (!instance || !instance.getReferencedComponent()) {
        fail("reference memory mapping requires a component instance");
        invalidReference = true; return;
      }
      auto callee = instance.getReferencedComponent();
      std::set<std::string> boundReferences;
      for (Attribute attribute : invoke.getRefCellsMap()) {
        auto mapping = dyn_cast<DictionaryAttr>(attribute);
        if (!mapping) {
          fail("invalid reference memory mapping"); invalidReference = true; return;
        }
        for (auto entry : mapping) {
          auto physical = dyn_cast<FlatSymbolRefAttr>(entry.getValue());
          if (!physical) {
            fail("invalid reference memory mapping"); invalidReference = true; return;
          }
          auto destination = contracts.find(Key{callee.getName().str(), entry.getName().str()});
          auto source = contracts.find(Key{caller.getName().str(), physical.getValue().str()});
          if (destination == contracts.end() || source == contracts.end()) {
            fail("reference memory mapping names an absent binding");
            invalidReference = true; return;
          }
          auto targetCell = cells.at(Key{callee.getName().str(), entry.getName().str()});
          if (targetCell->hasAttr("external")) {
            fail("reference memory mapping targets a physical cell");
            invalidReference = true; return;
          }
          if (!boundReferences.insert(entry.getName().str()).second) {
            fail("duplicate reference memory mapping");
            invalidReference = true; return;
          }
          for (StringRef field : {"width", "depth", "address_width", "implementation",
                                  "read_latency", "writable", "storage_width", "sign_extend",
                                  "value_width", "value_sign_extend"}) {
            if (destination->second.get(field) != source->second.get(field)) {
              fail("reference memory contract mismatch");
              invalidReference = true; return;
            }
          }
        }
      }
      if (boundReferences != requiredReferences[callee.getName().str()]) {
        fail("reference memory mapping leaves an unbound cell");
        invalidReference = true;
      }
    });
    if (invalidReference) return;
    // Nothing is changed until every binding and image has been validated.
    for (auto &[memory, attributes] : validated)
      memory->setAttr("llm2fpga.binding", attributes);
    getOperation()->setAttr("llm2fpga.binding_manifest_sha256",
                           builder.getStringAttr(manifestHash));
  }
};
} // namespace

namespace {
struct NarrowImmutableReads : PassWrapper<NarrowImmutableReads, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(NarrowImmutableReads)
  NarrowImmutableReads() = default;
  NarrowImmutableReads(const NarrowImmutableReads &other) : PassWrapper(other) {}
  Option<std::string> manifest{*this, "manifest"};
  Option<std::string> manifestHash{*this, "manifest-sha256"};
  StringRef getArgument() const final { return "llm2fpga-narrow-immutable-read-values"; }
  StringRef getDescription() const final {
    return "Expose lossless immutable read widths with typed slice/extension cells";
  }
  void getDependentDialects(DialectRegistry &registry) const override {
    registry.insert<calyx::CalyxDialect, hw::HWDialect>();
  }
  void runOnOperation() override {
    // Authenticate the current IR, images and all reference groups again.
    // An attribute from an earlier or unrelated validation is insufficient.
    OpPassManager validation("builtin.module");
    auto bindings = std::make_unique<MemoryBindings>();
    bindings->manifest = manifest.getValue();
    bindings->manifestHash = manifestHash.getValue();
    validation.addPass(std::move(bindings));
    if (failed(runPipeline(validation, getOperation()))) {
      signalPassFailure(); return;
    }
    SmallVector<calyx::SeqMemoryOp> memories;
    getOperation().walk([&](calyx::SeqMemoryOp memory) { memories.push_back(memory); });
    unsigned serial = 0, count = 0;
    for (auto memory : memories) {
      auto binding = memory->getAttrOfType<DictionaryAttr>("llm2fpga.binding");
      auto bits = binding.getAs<IntegerAttr>("value_width");
      if (!bits || bits.getInt() == static_cast<int64_t>(memory.getWidth())) continue;
      auto component = memory->getParentOfType<calyx::ComponentOp>();
      std::string sliceName, extendName;
      do {
        sliceName = "read_value_slice_" + std::to_string(serial);
        extendName = "read_value_extend_" + std::to_string(serial++);
      } while (component.lookupSymbol(sliceName) || component.lookupSymbol(extendName));
      OpBuilder builder(memory);
      auto wideType = memory.readData().getType();
      auto narrowType = builder.getIntegerType(bits.getInt());
      auto slice = calyx::SliceLibOp::create(builder, memory.getLoc(), sliceName,
          TypeRange{wideType, narrowType});
      Value input, output;
      if (binding.getAs<BoolAttr>("value_sign_extend").getValue()) {
        auto extend = calyx::ExtSILibOp::create(builder, memory.getLoc(), extendName,
            TypeRange{narrowType, wideType});
        input = extend.getIn(); output = extend.getOut();
      } else {
        auto extend = calyx::PadLibOp::create(builder, memory.getLoc(), extendName,
            TypeRange{narrowType, wideType});
        input = extend.getIn(); output = extend.getOut();
      }
      // Replace old consumers before adding the new source connection, so the
      // adapter never becomes a self-referential combinational loop.
      memory.readData().replaceAllUsesWith(output);
      builder.setInsertionPointToStart(&component.getWiresOp()->getRegion(0).front());
      calyx::AssignOp::create(builder, memory.getLoc(), slice.getIn(), memory.readData());
      calyx::AssignOp::create(builder, memory.getLoc(), input, slice.getOut());
      ++count;
    }
    getOperation()->setAttr("llm2fpga.narrowed_read_cells",
        IntegerAttr::get(IntegerType::get(&getContext(), 64), count));
  }
};
}

void registerMemoryBindingPass() {
  PassRegistration<MemoryBindings>();
  PassRegistration<NarrowImmutableReads>();
}

namespace {
// Materialize compiler-owned primitives while all users still hold typed SSA
// ports. No emitted Futil or SystemVerilog is inspected or modified here.
struct BindModelMemories
    : PassWrapper<BindModelMemories, OperationPass<ModuleOp>> {
  MLIR_DEFINE_EXPLICIT_INTERNAL_INLINE_TYPE_ID(BindModelMemories)
  BindModelMemories() = default;
  BindModelMemories(const BindModelMemories &other) : PassWrapper(other) {}
  Option<std::string> source{*this, "primitive-source"};
  Option<std::string> sourceHash{*this, "primitive-source-sha256"};
  StringRef getArgument() const final { return "llm2fpga-bind-model-memories"; }
  StringRef getDescription() const final {
    return "Replace authenticated sequential memories with typed owned primitives";
  }
  void runOnOperation() override {
    auto module = getOperation();
    auto fail = [&](StringRef message) {
      module.emitError() << "memory binding: " << message;
      signalPassFailure();
    };
    auto file = llvm::MemoryBuffer::getFile(source);
    if (!file || sha256((*file)->getBuffer()) != sourceHash) {
      fail("primitive source identity mismatch"); return;
    }
    if (!module->hasAttr("llm2fpga.binding_manifest_sha256")) {
      fail("validated memory manifest required"); return;
    }
    SmallVector<calyx::SeqMemoryOp> memories;
    std::set<std::string> kinds, images;
    bool invalid = false;
    module.walk([&](calyx::SeqMemoryOp memory) {
      auto binding = memory->getAttrOfType<DictionaryAttr>("llm2fpga.binding");
      if (!binding) { invalid = true; return; }
      for (StringRef field : {"width", "depth", "address_width"}) {
        auto number = binding.getAs<IntegerAttr>(field);
        if (!number || number.getInt() <= 0 || number.getInt() > INT32_MAX)
          invalid = true;
      }
      if (invalid) return;
      auto kind = binding.getAs<StringAttr>("implementation");
      if (!kind || !(kind.getValue() == "register" || kind.getValue() == "bram" ||
                     kind.getValue() == "lutram" || kind.getValue() == "ddr3") ||
          !binding.getAs<BoolAttr>("writable")) { invalid = true; return; }
      auto storage = binding.getAs<IntegerAttr>("storage_width");
      if (kind.getValue() == "ddr3") {
        auto latency = binding.getAs<IntegerAttr>("read_latency");
        if (!latency || latency.getInt() != 0 || storage ||
            binding.getAs<BoolAttr>("writable").getValue()) {
          memory.emitError("off-chip binding requires immutable full-width storage and dynamic completion");
          invalid = true; return;
        }
        if (memory->hasAttr("external")) {
          auto entrypoint = module->getAttrOfType<StringAttr>("calyx.entrypoint");
          auto component = memory->getParentOfType<calyx::ComponentOp>();
          if (!entrypoint || component.getName() != entrypoint.getValue()) {
            memory.emitError("physical off-chip memory must belong to entrypoint");
            invalid = true; return;
          }
        }
      }
      if (storage && (storage.getInt() <= 0 ||
          storage.getInt() > binding.getAs<IntegerAttr>("width").getInt() ||
          !binding.getAs<BoolAttr>("sign_extend") ||
          binding.getAs<BoolAttr>("writable").getValue())) {
        invalid = true; return;
      }
      if (!storage && binding.get("sign_extend")) { invalid = true; return; }
      kinds.insert(kind.getValue().str() + (storage ? "_narrow" : ""));
      if (auto image = binding.getAs<StringAttr>("init_file"))
        images.insert(image.getValue().str());
      memories.push_back(memory);
    });
    if (invalid) { fail("validated primitive parameters required within i32 range"); return; }
    auto primitiveName = [](StringRef kind) {
      bool narrow = kind.ends_with("_narrow");
      return "closure_" + (narrow ? kind.drop_back(7).str() : kind.str()) +
          "_memory" + (narrow ? "_narrow" : "");
    };
    for (auto &kind : kinds)
      if (SymbolTable::lookupSymbolIn(module, primitiveName(kind))) {
        fail("primitive declaration already exists"); return;
      }
    // Initializer IDs are stable under traversal order, matching source emission.
    std::map<std::string, int32_t> imageIds;
    int32_t nextId = 1;
    for (auto &image : images) imageIds.emplace(image, nextId++);
    OpBuilder builder(&getContext());
    builder.setInsertionPointToStart(module.getBody());
    for (auto &kind : kinds) {
      bool narrow = StringRef(kind).ends_with("_narrow");
      std::string name = primitiveName(kind);
      SmallVector<Attribute> declarations;
      for (StringRef parameter : {"WIDTH", "SIZE", "IDX_SIZE", "INIT_ID"})
        declarations.push_back(hw::ParamDeclAttr::get(parameter, builder.getI32Type()));
      if (narrow) {
        for (StringRef parameter : {"STORAGE_WIDTH", "SIGN_EXTEND"})
          declarations.push_back(hw::ParamDeclAttr::get(parameter, builder.getI32Type()));
      } else
        declarations.push_back(hw::ParamDeclAttr::get("WRITABLE", builder.getI32Type()));
      auto parameterType = [&](StringRef name) -> Type {
        return hw::IntType::get(hw::ParamDeclRefAttr::get(
            builder.getStringAttr(name), builder.getI32Type()));
      };
      SmallVector<hw::PortInfo> ports;
      auto port = [&](StringRef name, Type type, hw::ModulePort::Direction direction,
                      StringRef flag = {}) {
        hw::PortInfo info;
        info.name = builder.getStringAttr(name);
        info.type = type;
        info.dir = direction;
        info.argNum = ports.size();
        NamedAttrList attributes;
        if (!flag.empty()) attributes.set(flag, builder.getUnitAttr());
        // Calyx must know the request interval to schedule consecutive groups.
        // An unannotated dynamic read can observe the preceding write's done.
        if (name == "content_en") {
          attributes.set("calyx.go", builder.getI64IntegerAttr(1));
          if (kind != "ddr3")
            attributes.set("calyx.interval", builder.getI64IntegerAttr(
                StringRef(kind).starts_with("register") ? 1 : 2));
        }
        if (name == "done")
          attributes.set("calyx.done", builder.getI64IntegerAttr(1));
        info.attrs = attributes.getDictionary(&getContext());
        info.loc = module.getLoc();
        ports.push_back(info);
      };
      using Direction = hw::ModulePort::Direction;
      port("addr0", parameterType("IDX_SIZE"), Direction::Input);
      port("clk", builder.getI1Type(), Direction::Input, "calyx.clk");
      port("reset", builder.getI1Type(), Direction::Input, "calyx.reset");
      port("content_en", builder.getI1Type(), Direction::Input, "calyx.go");
      port("write_en", builder.getI1Type(), Direction::Input);
      port("write_data", parameterType("WIDTH"), Direction::Input);
      port("read_data", parameterType("WIDTH"), Direction::Output, "calyx.stable");
      port("done", builder.getI1Type(), Direction::Output, "calyx.done");
      auto declaration = hw::HWModuleExternOp::create(
          builder, module.getLoc(), builder.getStringAttr(name),
          ports, StringRef(), builder.getArrayAttr(declarations));
      declaration->setAttr("filename", builder.getStringAttr(source));
    }
    for (auto memory : memories) {
      auto binding = memory->getAttrOfType<DictionaryAttr>("llm2fpga.binding");
      SmallVector<Attribute> parameters;
      auto parameter = [&](StringRef name, int64_t value) {
        parameters.push_back(hw::ParamDeclAttr::get(name, builder.getI32IntegerAttr(value)));
      };
      parameter("WIDTH", binding.getAs<IntegerAttr>("width").getInt());
      parameter("SIZE", binding.getAs<IntegerAttr>("depth").getInt());
      parameter("IDX_SIZE", binding.getAs<IntegerAttr>("address_width").getInt());
      auto image = binding.getAs<StringAttr>("init_file");
      parameter("INIT_ID", image ? imageIds.at(image.getValue().str()) : 0);
      auto storage = binding.getAs<IntegerAttr>("storage_width");
      if (storage) {
        parameter("STORAGE_WIDTH", storage.getInt());
        parameter("SIGN_EXTEND", binding.getAs<BoolAttr>("sign_extend").getValue());
      } else
        parameter("WRITABLE", binding.getAs<BoolAttr>("writable").getValue());
      builder.setInsertionPoint(memory);
      auto primitive = calyx::PrimitiveOp::create(
          builder, memory.getLoc(), memory->getResultTypes(), memory.instanceName(),
          builder.getArrayAttr(parameters),
          "closure_" + binding.getAs<StringAttr>("implementation").getValue().str() +
          "_memory" + (storage ? "_narrow" : ""));
      primitive->setAttr("llm2fpga.binding", binding);
      primitive->setAttr("llm2fpga.ref", builder.getBoolAttr(!memory->hasAttr("external")));
      // Native Calyx externalization supplies the top-level request ports.
      // No DDR storage primitive is instantiated or initialized in the RTL.
      if (binding.getAs<StringAttr>("implementation").getValue() == "ddr3" &&
          memory->hasAttr("external"))
        primitive->setAttr("external", builder.getBoolAttr(true));
      // Downstream interface passes identify buffers by semantic role.
      if (auto role = memory->getAttrOfType<StringAttr>("llm2fpga.runtime_port"))
        primitive->setAttr("llm2fpga.runtime_port", role);
      memory->replaceAllUsesWith(primitive->getResults());
      memory.erase();
    }
    module->setAttr("llm2fpga.primitive_source_sha256", builder.getStringAttr(sourceHash));
  }
};
} // namespace
void registerModelMemoryBindingPass() { PassRegistration<BindModelMemories>(); }
