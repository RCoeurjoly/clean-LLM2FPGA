"""Native checkpoint mapping; a coarse checkpoint is not mapped BRAM evidence."""
import json
import gzip
from pathlib import Path
import shutil
import tempfile
import unittest

from compiler.stages import StageSpec, digest, run_stage, verify_stage_receipt

class Xc7MappingTests(unittest.TestCase):

    def test_serialized_outputs_reject_empty_gzip_payload(self):
        from scripts.pipeline.map_graph_core_xc7 import validate_serialized_outputs
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with gzip.open(root / 'checkpoint.il.gz', 'wb'):
                pass
            with self.assertRaises(ValueError):
                validate_serialized_outputs(root, ('checkpoint.il.gz',))

    def test_serialized_outputs_reject_truncated_gzip_payload(self):
        from scripts.pipeline.map_graph_core_xc7 import validate_serialized_outputs
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'checkpoint.il.gz'
            path.write_bytes(gzip.compress(b'nonempty checkpoint')[:-4])
            with self.assertRaises(ValueError):
                validate_serialized_outputs(root, ('checkpoint.il.gz',))

    def test_native_empty_checkpoint_cannot_be_qualified(self):
        from scripts.pipeline.map_graph_core_xc7 import qualify_checkpoint
        from compiler.stages import StageError
        yosys = shutil.which('yosys')
        if yosys is None:
            self.skipTest('native Yosys unavailable')
        yosys = Path(yosys).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            checkpoint = root / 'empty.il'
            checkpoint.write_text('# empty but syntactically valid RTLIL\n')
            with self.assertRaises(StageError):
                qualify_checkpoint(checkpoint, yosys, root / 'qualification',
                                   {str(checkpoint): digest(checkpoint)},
                                   {str(yosys): digest(yosys)}, final=False)
            self.assertFalse((root / 'qualification/success.json').exists())

    def test_native_compressed_outputs_roundtrip_through_yosys(self):
        import scripts.pipeline.map_graph_core_xc7 as mapping
        yosys = shutil.which('yosys')
        if yosys is None:
            self.skipTest('native Yosys unavailable')
        yosys = Path(yosys).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'identity.v'
            source.write_text('module main(input [63:0] a,output [63:0] q);assign q=a;endmodule\n')
            coarse = root / 'coarse'
            run_stage(StageSpec('compressed-netlist-input',
                (str(yosys), '-p', f'read_verilog {source}; '
                 'synth_xilinx -family xc7 -top main -noiopad -noclkbuf '
                 '-run begin:map_ffram; write_rtlil checkpoint.il'),
                {str(source): digest(source)}, {str(yosys): digest(yosys)}, {},
                ('checkpoint.il',)), coarse)
            final = root / 'final'
            mapping.map_checkpoint(coarse, 'checkpoint.il', yosys, final,
                                   phase='final', compress_outputs=True)
            verify_stage_receipt(final)
            verify_stage_receipt(final / 'serialized-checkpoint')
            with gzip.open(final / 'mapped.json.gz', 'rt') as stream:
                module = json.load(stream)['modules']['main']
            replay = root / 'replay'
            run_stage(StageSpec('compressed-native-replay',
                (str(yosys), '-p', f'read_rtlil {final}/checkpoint.il.gz; '
                 'check -assert; scc -expect 0; write_json replay.json'),
                verify_stage_receipt(final), {str(yosys): digest(yosys)}, {},
                ('replay.json',)), replay)
            self.assertEqual(json.loads((replay / 'replay.json').read_text())['modules']['main'], module)
            mapping.verify_mapped_checkpoint(final)
            mapping_spec = json.loads((final / 'spec.json').read_text())
            mapping.qualify_checkpoint(final / 'checkpoint.il.gz', yosys,
                final / 'serialized-checkpoint', verify_stage_receipt(final),
                mapping_spec['tool_hashes'], final=False)
            with self.assertRaises(ValueError):
                mapping.verify_mapped_checkpoint(final)
            mapping.qualify_checkpoint(final / 'checkpoint.il.gz', yosys,
                final / 'serialized-checkpoint', verify_stage_receipt(final),
                mapping_spec['tool_hashes'], final=True)
            (final / 'serialized-checkpoint/utilization.json').write_text('{}')
            with self.assertRaises(ValueError):
                mapping.verify_mapped_checkpoint(final)

    def test_native_ram_phase_rejects_changed_checkpoint(self):
        import scripts.pipeline.map_graph_core_xc7 as mapping
        yosys = shutil.which('yosys')
        if yosys is None:
            self.skipTest('native Yosys unavailable')
        yosys = Path(yosys).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'memory.v'
            source.write_text('''module main(input clk, input we,
                input [8:0] address, input [31:0] data, output reg [31:0] q);
                reg [31:0] memory [0:511];
                always @(posedge clk) begin
                    if (we) memory[address] <= data;
                    q <= memory[address];
                end
                endmodule
            ''')
            coarse = root/'coarse'
            run_stage(StageSpec('test-coarse-memory', (str(yosys), '-p',
                f'read_verilog {source}; synth_xilinx -family xc7 -top main '
                '-noiopad -noclkbuf -run begin:map_memory; '
                'write_rtlil checkpoint.il'),
                {str(source): digest(source)}, {str(yosys): digest(yosys)}, {},
                ('checkpoint.il',)), coarse)
            output = root/'mapped'
            mapping.map_checkpoint(coarse, 'checkpoint.il', yosys, output, phase='ram')
            (coarse/'checkpoint.il').write_text('altered checkpoint')
            with self.assertRaisesRegex(ValueError, 'stage output changed'):
                mapping.map_checkpoint(coarse, 'checkpoint.il', yosys, output, phase='ram')
            self.assertFalse((output/'success.json').exists())

    def test_native_coarse_stage_authenticates_rtl_and_readback(self):
        import sys
        import scripts.pipeline.map_graph_core_xc7 as mapping
        yosys = shutil.which('yosys')
        if yosys is None:
            self.skipTest('native Yosys unavailable')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'source.sv'
            source.write_text('module main(input [31:0] a,output [31:0] q); assign q=a; endmodule')
            stage = root/'rtl'
            run_stage(StageSpec('fixture-native-rtl', (sys.executable, '-c',
                'import shutil,sys;shutil.copyfile(sys.argv[1],"kernel.sv")', str(source)),
                {str(source): digest(source)}, {sys.executable: digest(Path(sys.executable))},
                {}, ('kernel.sv',)), stage)
            output = root/'coarse'
            mapping.coarse_checkpoint(stage, 'kernel.sv', Path(yosys), output)
            (output/'serialized-checkpoint/utilization.json').write_text('{}')
            with self.assertRaisesRegex(ValueError, 'stage output changed'):
                mapping.map_checkpoint(output, 'checkpoint.il.gz', Path(yosys),
                                       root/'ram', phase='ram')
            self.assertFalse((root/'ram/success.json').exists())
            mapping.coarse_checkpoint(stage, 'kernel.sv', Path(yosys), output)
            (stage/'kernel.sv').write_text('altered')
            with self.assertRaisesRegex(ValueError, 'stage output changed'):
                mapping.coarse_checkpoint(stage, 'kernel.sv', Path(yosys), output)
            self.assertFalse((output/'success.json').exists())

    def test_input_stage_cannot_be_overwritten(self):
        import scripts.pipeline.map_graph_core_xc7 as mapping
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            success = root/'success.json'
            success.write_text('input evidence')
            with self.assertRaisesRegex(ValueError, 'separate'):
                mapping.map_checkpoint(root, 'checkpoint.il', Path('/missing/tool'),
                                       root, phase='ram')
            self.assertEqual(success.read_text(), 'input evidence')

if __name__ == '__main__':
    unittest.main()
