module {
  func.func @main(%arg0: f32, %arg1: f32, %arg2: f32) -> f32 {
    %0 = math.fma %arg0, %arg1, %arg2 : f32
    return %0 : f32
  }
}
