module {
  func.func @main(%arg0: f64) -> f32 {
    %0 = arith.truncf %arg0 : f64 to f32
    return %0 : f32
  }
}
