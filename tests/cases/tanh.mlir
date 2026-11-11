module {
  func.func @main(%arg0: f32) -> f32 {
    %0 = math.tanh %arg0 : f32
    return %0 : f32
  }
}
