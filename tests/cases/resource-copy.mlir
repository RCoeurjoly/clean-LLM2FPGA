module {
  memref.global "private" constant @weight : memref<1xf32> = dense_resource<weight>
  func.func @main() -> f32 {
    %memory = memref.get_global @weight : memref<1xf32>
    %zero = arith.constant 0 : index
    %destination = memref.alloc() : memref<1xf32>
    memref.copy %memory, %destination : memref<1xf32> to memref<1xf32>
    %value = memref.load %destination[%zero] : memref<1xf32>
    return %value : f32
  }
}
{-# dialect_resources: { builtin: { weight: "0x04000000ecd18c3f" } } #-}
