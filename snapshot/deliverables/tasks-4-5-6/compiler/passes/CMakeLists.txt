cmake_minimum_required(VERSION 3.20)
project(LLM2FPGATorchPasses LANGUAGES C CXX)
find_package(MLIR REQUIRED CONFIG)
if(NOT TORCH_MLIR_ROOT)
  message(FATAL_ERROR "TORCH_MLIR_ROOT must select the pinned shared-MLIR SDK")
endif()
find_library(TORCH_CAPI NAMES TorchMLIRAggregateCAPI REQUIRED
  PATHS "${TORCH_MLIR_ROOT}/lib/python3.11/site-packages/torch_mlir/torch_mlir/_mlir_libs"
  NO_DEFAULT_PATH)
add_executable(llm2fpga-torch-opt torch/Driver.cpp torch/ExactIntegerPasses.cpp
  torch/SerialGemvLowering.cpp torch/StandardIntegerBoundary.cpp torch/TestScalarEntry.cpp torch/IntegerMath.cpp torch/IntegerStrengthReduction.cpp torch/InputOrigins.cpp torch/BufferReuse.cpp torch/FunctionSharing.cpp torch/ProducerSlices.cpp torch/PointwiseFusion.cpp torch/GenerationLink.cpp torch/GenerationImport.cpp torch/HorizontalPointwiseFusion.cpp)
target_sources(llm2fpga-torch-opt PRIVATE torch/PrivateStorage.cpp)
target_sources(llm2fpga-torch-opt PRIVATE torch/LoopOutlining.cpp torch/RestoringSqrtLoops.cpp torch/GemvEpilogue.cpp)
target_compile_features(llm2fpga-torch-opt PRIVATE cxx_std_17)
target_include_directories(llm2fpga-torch-opt PRIVATE ${MLIR_INCLUDE_DIRS} ${LLVM_INCLUDE_DIRS} "${TORCH_MLIR_ROOT}/include")
target_link_libraries(llm2fpga-torch-opt PRIVATE MLIR LLVM ${TORCH_CAPI})
# Preserve the external SDK library paths in the installed executable. The
# install check executes without LD_LIBRARY_PATH, including Torch's shared C API.
set_target_properties(llm2fpga-torch-opt PROPERTIES INSTALL_RPATH_USE_LINK_PATH TRUE)
install(TARGETS llm2fpga-torch-opt RUNTIME DESTINATION bin)
