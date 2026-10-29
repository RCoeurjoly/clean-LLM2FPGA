"""Native SSA coalescing must preserve pointwise ownership/index contracts."""
import os
import subprocess
import unittest


def fixture():
    return '''module {

      func.func @main(%out: memref<4xi64>) {
        %zero = arith.constant 0 : i64
        %one = arith.constant 1 : i64
        %idx = arith.constant 0 : index
        %n = arith.constant 4 : index
        %a = memref.alloc() {alignment = 64 : i64} : memref<4xi64>
        %b = memref.alloc() {alignment = 64 : i64} : memref<4xi64>
        linalg.fill ins(%zero : i64) outs(%a : memref<4xi64>)

        linalg.generic {indexing_maps = [affine_map<(d0)->(d0)>, affine_map<(d0)->(d0)>], iterator_types = ["parallel"]} ins(%a : memref<4xi64>) outs(%b : memref<4xi64>) {
      ^bb0(%x: i64, %unused: i64):

        %v = arith.addi %x, %one : i64
        linalg.yield %v : i64
    }

         linalg.generic {indexing_maps = [affine_map<(d0)->(d0)>, affine_map<(d0)->(d0)>], iterator_types = ["parallel"]} ins(%b : memref<4xi64>) outs(%a : memref<4xi64>) {
      ^bb0(%y: i64, %old: i64):
        %v2 = arith.addi %y, %one : i64
        linalg.yield %v2 : i64
    }


        memref.copy %a, %out : memref<4xi64> to memref<4xi64>
        return
      }
    }'''


class PointwisePingPongTests(unittest.TestCase):
    def test_native_execution_preserves_wrapping_values(self):
        from test_graph_sequential_scratch import SequentialScratchTests
        values = [-(1 << 63), -1, 0, 1, (1 << 63)-2, (1 << 63)-1]
        for value in values:
            with self.subTest(value=value):
                source = fixture().replace('%zero = arith.constant 0 : i64',
                                           f'%zero = arith.constant {value} : i64')
                # A separate entrypoint calls the actual transformed buffer maps.
                source = source.replace('func.func @main(', 'func.func @maps(')
                expected = ((value + 2 + (1 << 63)) % (1 << 64)) - (1 << 63)
                source = source.rsplit('}', 1)[0] + f"""
                  func.func @main() -> i64 {{
                    %out = memref.alloc() : memref<4xi64>
                    func.call @maps(%out) : (memref<4xi64>) -> ()
                    %idx = arith.constant 3 : index
                    %got = memref.load %out[%idx] : memref<4xi64>
                    return %got : i64
                  }} }}"""
                result = subprocess.run([os.environ['LLM2FPGA_TORCH_PASS_TOOL'],
                    '--verify-each', '--llm2fpga-coalesce-pointwise-pingpong'],
                    input=source, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                executor = SequentialScratchTests()
                self.assertEqual(executor.execute(source), expected)
                self.assertEqual(executor.execute(result.stdout), expected)


if __name__=='__main__':unittest.main()
