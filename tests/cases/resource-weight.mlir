module {
  memref.global "private" constant @weight : memref<1xf32> = dense_resource<weight>
  func.func @main() -> f32 {
    %memory = memref.get_global @weight : memref<1xf32>
    %zero = arith.constant 0 : index
    %value = memref.load %memory[%zero] : memref<1xf32>
    return %value : f32
  }
}
{-# dialect_resources: { builtin: { weight: "0x04000000ecd18c3f" } } #-}
