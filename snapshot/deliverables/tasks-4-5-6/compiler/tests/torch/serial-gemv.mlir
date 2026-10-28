module {
  func.func @gemv(%x: !torch.vtensor<[1,3],si64>, %w: !torch.vtensor<[2,3],si64>) -> !torch.vtensor<[1,2],si64> {
    %r = torch.operator "torch.llm2fpga.serial_gemv"(%x, %w) : (!torch.vtensor<[1,3],si64>, !torch.vtensor<[2,3],si64>) -> !torch.vtensor<[1,2],si64>
    return %r : !torch.vtensor<[1,2],si64>
  }
}
