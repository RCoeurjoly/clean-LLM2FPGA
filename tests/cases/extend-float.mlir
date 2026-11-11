module {
  func.func @main(%arg0: f32) -> f64 {
    %0 = arith.extf %arg0 : f32 to f64
    return %0 : f64
  }
}
