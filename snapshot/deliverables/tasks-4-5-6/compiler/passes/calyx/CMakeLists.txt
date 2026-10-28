cmake_minimum_required(VERSION 3.20)
project(LLM2FPGACalyxPasses LANGUAGES C CXX)
find_package(MLIR REQUIRED CONFIG)
find_package(CIRCT REQUIRED CONFIG)
set(LLM2FPGA_SCF_TO_CALYX_SOURCE "${CMAKE_CURRENT_SOURCE_DIR}/SCFToCalyx.cpp" CACHE FILEPATH
    "Pinned SCF-to-Calyx source with graph-input origin preservation")
add_executable(llm2fpga-calyx-opt Driver.cpp ExclusiveCombInputs.cpp IdenticalComb.cpp RegisterBoundedComb.cpp StaticLoopCounters.cpp TransactionalDividers.cpp ExternalMemoryInterface.cpp MemoryContracts.cpp MemoryBindings.cpp MemoryInventory.cpp MemoryRetirement.cpp RuntimeMemories.cpp RuntimeInterface.cpp
    ${LLM2FPGA_SCF_TO_CALYX_SOURCE} FlattenMemRefs.cpp)
# Match the SDK's dialect lowering classes. Compile the selected pass source
# once; linking CIRCTSCFToCalyx would also pull its original implementation.
target_compile_options(llm2fpga-calyx-opt PRIVATE -fno-rtti)
target_compile_features(llm2fpga-calyx-opt PRIVATE cxx_std_17)
target_include_directories(llm2fpga-calyx-opt PRIVATE ${MLIR_INCLUDE_DIRS} ${LLVM_INCLUDE_DIRS} ${CIRCT_INCLUDE_DIRS})
target_link_libraries(llm2fpga-calyx-opt PRIVATE CIRCTCalyxTransforms CIRCTPipelineOps CIRCTCalyx CIRCTHW CIRCTComb MLIROptLib
    MLIRAffineToStandard MLIRTransforms MLIRArithDialect MLIRFuncDialect
    MLIRMemRefDialect MLIRSCFDialect MLIRMathDialect MLIRControlFlowDialect MLIRControlFlowTransforms)
set_target_properties(llm2fpga-calyx-opt PROPERTIES INSTALL_RPATH_USE_LINK_PATH TRUE)
install(TARGETS llm2fpga-calyx-opt RUNTIME DESTINATION bin)
set(LLM2FPGA_CALYX_EMITTER_SOURCE "${CMAKE_CURRENT_SOURCE_DIR}/CalyxEmitter.cpp" CACHE FILEPATH
    "Pinned CIRCT emitter source with the reference-primitive patch")
add_executable(llm2fpga-calyx-translate Translate.cpp ${LLM2FPGA_CALYX_EMITTER_SOURCE})
target_compile_features(llm2fpga-calyx-translate PRIVATE cxx_std_17)
target_include_directories(llm2fpga-calyx-translate PRIVATE ${MLIR_INCLUDE_DIRS} ${LLVM_INCLUDE_DIRS} ${CIRCT_INCLUDE_DIRS})
target_link_libraries(llm2fpga-calyx-translate PRIVATE CIRCTCalyx CIRCTHW CIRCTComb MLIRTranslateLib)
set_target_properties(llm2fpga-calyx-translate PROPERTIES INSTALL_RPATH_USE_LINK_PATH TRUE)
install(TARGETS llm2fpga-calyx-translate RUNTIME DESTINATION bin)
