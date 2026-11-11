import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.bind_memories import image_bits


class ModelMemoryTests(unittest.TestCase):
    def test_images_survive_reads_and_runtime_writes(self):
        cases = [(32, 'ieee754_float', 1.100156307220459, 0x3f8cd1ec),
                 (64, 'ieee754_float', 1.0000000000000002, 0x3ff0000000000001),
                 (8, 'bitnum', -7, 0xf9)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for width, kind, value, expected in cases:
                with self.subTest(width=width, kind=kind):
                    bits = image_bits(value, dict(width=width, numeric_type=kind))
                    (root / 'image.hex').write_text(f'{bits:x}\n0\n')
                    script = (
                        f'read_verilog -sv "{os.environ["MODEL_MEMORY"]}"; '
                        f'chparam -set WIDTH {width} -set SIZE 2 -set ADDR 1 '
                        f'-set IMAGE "{root}/image.hex" model_memory; '
                        'hierarchy -check -top model_memory; proc; memory_collect; '
                        'opt_dff *model_memory; select *; flatten -noscopeinfo; '
                        'opt_clean; memory_dff; '
                        'submod -name model_memories t:$mem_v2; select *; '
                        'write_verilog -noattr lowered.sv; '
                        'synth_xilinx -family xc7 -top model_memory -noiopad -noclkbuf '
                        '-run begin:prepare; memory -nomap; '
                        'synth_xilinx -family xc7 -top model_memory -noiopad -noclkbuf '
                        '-run map_memory:map_ffram; flatten -noscopeinfo; check -assert; '
                        'blackbox =A:whitebox; write_json mapped.json')
                    result = subprocess.run([os.environ['YOSYS'], '-Q', '-T', '-p', script],
                        cwd=root, capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    (root / 'tb.sv').write_text(f'''module tb;
reg clk=0,reset=1,content_en=0,write_en=0,addr0=0;
reg [{width-1}:0] write_data=0;wire [{width-1}:0] read_data;wire done;
model_memory dut(.*);
always #5 clk=~clk;
initial begin
repeat(2)@(negedge clk);reset=0;content_en=1;
@(negedge clk);
if(done!==1 || read_data!=={width}'h{expected:x}) $fatal(1,"checkpoint image lost");
addr0=1;write_en=1;write_data='h35;
@(negedge clk);write_en=0;
@(negedge clk);
if(read_data!=={width}'h35) $fatal(1,"runtime write lost");
addr0=0;
@(negedge clk);
if(read_data!=={width}'h{expected:x}) $fatal(1,"neighbour overwritten");
reset=1;
@(negedge clk);
if(done!==0 || read_data!==0) $fatal(1,"reset handshake");
$finish;end endmodule''')
                    for command in [
                        [os.environ['IVERILOG'], '-g2012', '-s', 'tb', '-o', str(root/'sim'),
                         str(root/'tb.sv'), str(root/'lowered.sv')],
                        [os.environ['VVP'], str(root/'sim')],
                    ]:
                        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
