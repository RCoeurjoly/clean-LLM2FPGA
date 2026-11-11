cmake_minimum_required(VERSION 3.20)
project(LLM2FPGACalyxPasses LANGUAGES C CXX)
find_package(MLIR REQUIRED CONFIG)
find_package(CIRCT REQUIRED CONFIG)
add_executable(llm2fpga-calyx-opt Driver.cpp PrepareArithmetic.cpp PrepareMath.cpp
    SCFToCalyx.cpp FlattenMemRefs.cpp)
target_compile_options(llm2fpga-calyx-opt PRIVATE -fno-rtti)
target_link_libraries(llm2fpga-calyx-opt PRIVATE CIRCTCalyxTransforms CIRCTPipelineOps
    CIRCTCalyx CIRCTHW CIRCTComb MLIROptLib MLIRAffineToStandard MLIRTransforms
    MLIRArithDialect MLIRFuncDialect MLIRMemRefDialect MLIRSCFDialect
    MLIRMathDialect MLIRMathTransforms MLIRControlFlowDialect MLIRControlFlowTransforms)
add_executable(llm2fpga-calyx-translate Translate.cpp CalyxEmitter.cpp)
target_link_libraries(llm2fpga-calyx-translate PRIVATE
    CIRCTCalyx CIRCTHW CIRCTComb MLIRTranslateLib)
foreach(target llm2fpga-calyx-opt llm2fpga-calyx-translate)
    target_compile_features(${target} PRIVATE cxx_std_17)
    target_include_directories(${target} PRIVATE
        ${MLIR_INCLUDE_DIRS} ${LLVM_INCLUDE_DIRS} ${CIRCT_INCLUDE_DIRS})
    set_target_properties(${target} PROPERTIES INSTALL_RPATH_USE_LINK_PATH TRUE)
endforeach()
install(TARGETS llm2fpga-calyx-opt llm2fpga-calyx-translate RUNTIME DESTINATION bin)
