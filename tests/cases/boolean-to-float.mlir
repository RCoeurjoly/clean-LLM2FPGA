module {
  func.func @main(%arg0: i1) -> f32 {
    %0 = arith.uitofp %arg0 : i1 to f32
    return %0 : f32
  }
}
