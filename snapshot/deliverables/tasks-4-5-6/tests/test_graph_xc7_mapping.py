"""Native checkpoint mapping; a coarse checkpoint is not mapped BRAM evidence."""
import json
import gzip
from pathlib import Path
import shutil
import tempfile
import unittest

from compiler.stages import StageSpec, digest, run_stage, verify_stage_receipt


class Xc7MappingTests(unittest.TestCase):
    def test_coarse_mapping_specializes_signed_comparator_before_technology_mapping(self):
        import scripts.pipeline.map_graph_core_xc7 as mapping
        yosys = shutil.which('yosys')
        if yosys is None:
            self.skipTest('native Yosys unavailable')
        yosys = Path(yosys).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'compare.sv'
            source.write_text('module std_sge #(parameter WIDTH=64)(input[WIDTH-1:0]left,right,output out);'
                              'assign out=$signed(left)>=$signed(right);endmodule\n'
                              'module main(input[63:0]x,input g,output y);'
                              'std_sge c(g?x:64\'d0,g?64\'d128:64\'d0,y);endmodule\n')
            parent = root / 'parent'
            # Preserve argv[0]: Nix coreutils can expose cp as a multicall symlink.
            copy = Path(shutil.which('cp'))
            run_stage(StageSpec('comparator-input',
                (str(copy), str(source), 'kernel.sv'),
                {str(source): digest(source)},
                {str(copy): digest(copy)},
                {}, ('kernel.sv',)), parent)
            mapping.coarse_checkpoint(parent, 'kernel.sv', yosys, root / 'coarse')
            mapping.map_checkpoint(root / 'coarse', 'checkpoint.il.gz', yosys,
                                   root / 'ram', phase='ram', compress_outputs=True)
            mapping.map_checkpoint(root / 'ram', 'checkpoint.il.gz', yosys,
                                   root / 'final', phase='final', compress_outputs=True)
            types = json.loads((root / 'final/utilization.json').read_text())['design']['num_cells_by_type']
            self.assertLessEqual(sum(types.get(f'LUT{i}', 0) for i in range(1, 7)), 24,
                                 'hierarchical comparator was expanded before specialization')

    def test_comparator_preparation_preserves_compute_and_memory_hierarchy(self):
        from compiler.yosys_pipeline import coarse_preparation_passes
        yosys = shutil.which('yosys')
        if yosys is None:
            self.skipTest('native Yosys unavailable')
        import subprocess
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'hierarchy.sv'
            source.write_text('module std_sge #(parameter WIDTH=64)(input[WIDTH-1:0]left,right,output out);'
                              'assign out=$signed(left)>=$signed(right);endmodule\n'
                              'module compute(input[63:0]x,input g,output y);'
                              'std_sge c(g?x:64\'d0,g?64\'d128:64\'d0,y);endmodule\n'
                              'module bank(input clk,we,input[3:0]a,input[7:0]d,output[7:0]q);'
                              'reg[7:0]m[0:15];always@(posedge clk)if(we)m[a]<=d;assign q=m[a];endmodule\n'
                              'module main(input clk,we,input[3:0]a,input[7:0]d,input[63:0]x,input g,output y,output[7:0]q);'
                              'compute c(x,g,y);bank b(clk,we,a,d,q);endmodule\n')
            script = '; '.join((f'read_verilog -sv {source}', *coarse_preparation_passes('main'),
                                'check -assert', 'select main compute bank',
                                f'write_json -selected {root}/coarse.json'))
            subprocess.run([yosys, '-Q', '-T', '-p', script], check=True,
                           capture_output=True, timeout=60)
            modules = json.loads((root / 'coarse.json').read_text())['modules']
            self.assertIn('compute', modules, 'whole compute hierarchy was flattened')
            self.assertIn('bank', modules, 'memory hierarchy was flattened')
            self.assertEqual({c['type'] for c in modules['main']['cells'].values()}, {'compute', 'bank'})

    def test_final_mapping_specializes_constant_shift_across_hierarchy(self):
        import scripts.pipeline.map_graph_core_xc7 as mapping
        yosys = shutil.which('yosys')
        if yosys is None:
            self.skipTest('native Yosys unavailable')
        yosys = Path(yosys).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'shift.sv'
            source.write_text('module shift(input signed [63:0] a,input [63:0] b,output signed [63:0] q);assign q=a>>>b;endmodule\n'
                              'module main(input signed [63:0] a,output signed [63:0] q);shift s(a,64\'d8,q);endmodule\n')
            parent = root / 'parent'
            run_stage(StageSpec('constant-shift-input',
                (str(yosys), '-p', f'read_verilog -sv {source}; synth_xilinx -family xc7 -top main -noiopad -noclkbuf -run begin:map_ffram; write_rtlil checkpoint.il'),
                {str(source): digest(source)}, {str(yosys): digest(yosys)}, {},
                ('checkpoint.il',)), parent)
            final = root / 'final'
            mapping.map_checkpoint(parent, 'checkpoint.il', yosys, final, phase='final')
            types = json.loads((final / 'utilization.json').read_text())['design']['num_cells_by_type']
            self.assertEqual(sum(n for k,n in types.items() if k.startswith('LUT')), 0,
                             'constant shift survived as a barrel shifter')

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
            self.assertFalse((final / 'checkpoint.il').exists())
            self.assertFalse((final / 'mapped.json').exists())
            with gzip.open(final / 'mapped.json.gz', 'rt') as stream:
                module = json.load(stream)['modules']['main']
            self.assertEqual(module['ports']['a']['bits'], module['ports']['q']['bits'])
            replay = root / 'replay'
            run_stage(StageSpec('compressed-native-replay',
                (str(yosys), '-p', f'read_rtlil {final}/checkpoint.il.gz; '
                 'check -assert; scc -expect 0; write_json replay.json'),
                verify_stage_receipt(final), {str(yosys): digest(yosys)}, {},
                ('replay.json',)), replay)
            self.assertEqual(json.loads((replay / 'replay.json').read_text())['modules']['main'], module)
            mapping.verify_mapped_checkpoint(final)
            from compiler.yosys_pipeline import top_flatten_passes
            policy = Path(top_flatten_passes.__code__.co_filename).resolve()
            self.assertEqual(verify_stage_receipt(final).get(str(policy)), digest(policy),
                             'final native pass policy is not authenticated')
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


    def test_native_sdp72_preserves_upper_64bit_write_data_parity(self):
        yosys = shutil.which('yosys')
        if yosys is None:
            self.skipTest('native Yosys unavailable')
        yosys = Path(yosys).resolve()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'wide-memory.v'
            source.write_text('''module main(input clk,input we,
                input [8:0] address,input [63:0] data,output reg [63:0] q);
                reg [63:0] memory[0:511];
                always @(posedge clk) begin
                    if(we) memory[address]<=data;
                    q<=memory[address];
                end
                endmodule
            ''')
            stage = root / 'mapped'
            run_stage(StageSpec('wide-memory-parity-regression',
                (str(yosys), '-p', f'read_verilog {source}; '
                 'synth_xilinx -family xc7 -top main -noiopad -noclkbuf; '
                 'write_json mapped.json'),
                {str(source): digest(source)}, {str(yosys): digest(yosys)}, {},
                ('mapped.json',)), stage)
            module = json.loads((stage / 'mapped.json').read_text())['modules']['main']
            memories = [cell for cell in module['cells'].values()
                        if cell['type'] == 'RAMB36E1']
            self.assertEqual(len(memories), 1)
            memory = memories[0]
            self.assertEqual(memory['parameters']['RAM_MODE'], 'SDP')
            self.assertEqual(int(memory['parameters']['WRITE_WIDTH_B'], 2), 72)
            data = module['ports']['data']['bits']
            # MAKE_DI packs each ninth logical bit into a parity pin. The
            # upper physical port must carry bits 44, 53 and 62, independently
            # of the lower parity bits 8, 17, 26 and 35.
            self.assertEqual(memory['connections']['DIPBDIP'][:3],
                             [data[44], data[53], data[62]])

    def test_native_ram_phase_maps_memory_and_rejects_changed_checkpoint(self):
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
                'write_rtlil checkpoint.il; tee -o coarse.json stat -json'),
                {str(source): digest(source)}, {str(yosys): digest(yosys)}, {},
                ('checkpoint.il', 'coarse.json')), coarse)
            before = json.loads((coarse/'coarse.json').read_text())
            self.assertIn('$mem_v2', before['modules']['\\main']['num_cells_by_type'])
            output = root/'mapped'
            mapping.map_checkpoint(coarse, 'checkpoint.il', yosys, output, phase='ram')
            verify_stage_receipt(output)
            after = json.loads((output/'utilization.json').read_text())
            types = set(after['modules']['\\main']['num_cells_by_type'])
            self.assertNotIn('$mem_v2', types)
            self.assertTrue(types & {'RAMB36E1', 'RAMB18E1'})
            final = root/'final'
            mapping.map_checkpoint(output, 'checkpoint.il', yosys, final, phase='final')
            final_dependencies = verify_stage_receipt(final)
            abc = yosys.with_name('yosys-abc')
            with self.subTest(mapping_executable='yosys-abc'):
                self.assertTrue(abc.is_file())
                self.assertEqual(final_dependencies.get(str(abc)), digest(abc),
                                 'native ABC executable absent from stage identity')
            netlist = json.loads((final/'mapped.json').read_text())
            self.assertIn('main', netlist['modules'])
            self.assertFalse(any(cell['type'].startswith('$')
                for cell in netlist['modules']['main']['cells'].values()))
            # The executable is not the whole synthesis tool: native passes
            # read technology definitions separately from that executable.
            data = yosys.parent.parent / 'share/yosys'
            for relative in ('xilinx/arith_map.v', 'xilinx/cells_map.v',
                             'xilinx/brams_xc4v_map.v'):
                library = data / relative
                with self.subTest(mapping_library=relative):
                    self.assertTrue(library.is_file())
                    self.assertEqual(final_dependencies.get(str(library)), digest(library),
                                     'native mapping library absent from stage identity')
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
            verify_stage_receipt(output)
            verify_stage_receipt(output/'serialized-checkpoint')
            spec = json.loads((output/'spec.json').read_text())
            passes = json.loads(spec['pass_identities']['native_passes'])
            self.assertIn('opt_clean -purge', passes,
                          'coarse preparation never retires public slice aliases')
            self.assertLess(passes.index('proc'), passes.index('opt_clean -purge'))
            self.assertLess(passes.index('opt_whole_wire_aliases'),
                            passes.index('opt_clean -purge'))
            self.assertLess(passes.index('proc'),
                            passes.index('opt_connection_helpers */t:*std_bit_slice*'))
            from compiler import yosys_pipeline
            policy = Path(yosys_pipeline.__file__).resolve()
            self.assertEqual(verify_stage_receipt(output).get(str(policy)), digest(policy),
                             'coarse native preparation policy is not authenticated')
            self.assertTrue((output/'checkpoint.il.gz').is_file())
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

    def test_native_prepared_coarse_preserves_memory_initialization(self):
        import subprocess
        import sys
        import scripts.pipeline.map_graph_core_xc7 as mapping
        yosys = shutil.which('yosys')
        if yosys is None:
            self.skipTest('native Yosys unavailable')
        values = [0, 1, 255, 128, 85, 170, 37, 219]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'initialized-memory.sv'
            initializers = ''.join(
                f'memory[{index}]=8\'d{value};' for index, value in enumerate(values)
            )
            source.write_text(
                'module main(input clk,we,input [2:0] address,input [7:0] data,'
                'output reg [7:0] q);reg [7:0] memory[0:7];initial begin '
                + initializers + ' end always @(posedge clk)begin '
                'if(we)memory[address]<=data;q<=memory[address];end endmodule'
            )
            stage = root / 'rtl'
            run_stage(StageSpec('initialized-memory-input', (sys.executable, '-c',
                'import shutil,sys;shutil.copyfile(sys.argv[1],"kernel.sv")', str(source)),
                {str(source): digest(source)}, {sys.executable: digest(Path(sys.executable))},
                {}, ('kernel.sv',)), stage)
            coarse = root / 'coarse'
            mapping.coarse_checkpoint(stage, 'kernel.sv', Path(yosys), coarse)
            mapping.verify_mapped_checkpoint(coarse)
            result = subprocess.run(
                [yosys, '-Q', '-T', '-p',
                 f'read_rtlil {coarse}/checkpoint.il.gz; hierarchy -check -top main; '
                 f'select main; write_json -selected {root}/memory.json'],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            module = json.loads((root / 'memory.json').read_text())['modules']['main']
            memories = [cell for cell in module['cells'].values() if cell['type'] == '$mem_v2']
            self.assertEqual(len(memories), 1)
            memory = memories[0]
            self.assertEqual(int(memory['parameters']['WIDTH'], 2), 8)
            self.assertEqual(int(memory['parameters']['SIZE'], 2), 8)
            packed = sum(value << (8 * index) for index, value in enumerate(values))
            self.assertEqual(int(memory['parameters']['INIT'], 2), packed)

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
