module {
  func.func @main(%arg0: f32, %arg1: f32) -> f32 {
    %0 = arith.maximumf %arg0, %arg1 : f32
    return %0 : f32
  }
}
