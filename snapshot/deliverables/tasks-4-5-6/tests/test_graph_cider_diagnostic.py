"""Exercise native private-memory metadata normalization with strict Cider."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

LIB='/nix/store/b3m8c75hw6hhswbky443nfk0b1rm8fx3-calyx-0.7.1/share/calyx'
CIDER=os.environ.get('LLM2FPGA_CIDER','/nix/store/y5bh6kdknbpx1wzmaq8g686ssqwx26r1-llm2fpga-cider-diagnostic-0.7.1/bin/cider')
CONVERTER='/nix/store/imv2vmma8n6q4g88bh3vz3n1d0ss957n-llm2fpga-cider-data-converter-0.7.1/bin/cider-data-converter'
PROGRAM='''import "primitives/core.futil";
import "primitives/memories/seq.futil";
component child() -> (result: 64) {
 cells { @external mem = seq_mem_d1(64, 1, 1); }
 wires { group write { mem.addr0 = 1'd0; mem.write_data = 64'd7;
   mem.write_en = 1'd1; mem.content_en = 1'd1; write[done] = mem.done; }
   group read { mem.addr0 = 1'd0; mem.write_en = 1'd0; mem.write_data = 64'd0;
     mem.content_en = 1'd1; read[done] = mem.done; }
   result = mem.read_data;
 }
 control { seq { write; read; } }
}
component main() -> () {
 cells { sub = child(); @external out = seq_mem_d1(64, 1, 1); }
 wires { group write { out.addr0 = 1'd0; out.write_data = sub.result;
   out.write_en = 1'd1; out.content_en = 1'd1; write[done] = out.done; } }
 control { seq { invoke sub()(); write; } }
}
'''


class CiderDiagnosticTests(unittest.TestCase):
    @unittest.skipUnless(Path(CIDER).is_file(),'requires pinned Cider diagnostic')
    def test_builtin_mux_dispatch_preserves_full_64_bit_patterns(self):
        pairs=[(0,2**64-1),(2**63,2**63-1),(0x0123456789abcdef,0xfedcba9876543210),(7,19)]
        groups=[];expected=[]
        for tru,fal in pairs:
            for cond in (0,1):
                i=len(expected);expected.append(tru if cond else fal)
                groups.append(f'''group g{i} {{ mux.cond = 1'd{cond}; mux.tru = 64'd{tru}; mux.fal = 64'd{fal};
                    out.addr0 = 3'd{i}; out.write_data = mux.out; out.content_en = 1'd1;
                    out.write_en = 1'd1; g{i}[done] = out.done; }}''')
        program='''import "primitives/core.futil";
import "primitives/memories/seq.futil";
component main() -> () {
 cells { mux = std_mux(64); @external out = seq_mem_d1(64, 8, 3); }
 wires { '''+'\n'.join(groups)+' }\n control { seq { '+''.join(f'g{i};' for i in range(8))+' } }\n}\n'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'mux.futil';source.write_text(program)
            run=subprocess.run([CIDER,str(source),'-l',LIB],capture_output=True,timeout=30)
            self.assertEqual(run.returncode,0,run.stderr)
            dump=root/'out.dump';dump.write_bytes(run.stdout)
            result=subprocess.run([CONVERTER,str(dump),'-t','json'],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['out'],expected)

    @unittest.skipUnless(os.environ.get('LLM2FPGA_CALYX_PRIVATE_NORMALIZER'),
                         'requires explicitly built retained-study diagnostic tools')
    def test_native_metadata_adapter_preserves_entry_memory_and_strict_execution(self):
        normalizer=os.environ.get('LLM2FPGA_CALYX_PRIVATE_NORMALIZER','/tmp/llm2fpga-normalizer-not-built')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'source.futil';source.write_text(PROGRAM)
            raw=subprocess.run([CIDER,str(source),'-l',LIB],capture_output=True,timeout=30)
            self.assertNotEqual(raw.returncode,0)
            self.assertIn(b'Cell cannot be marked',raw.stderr)
            result=subprocess.run([normalizer,str(source),LIB],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            normalized=root/'normalized.futil';normalized.write_text(result.stdout)
            self.assertEqual(source.read_text(),PROGRAM)
            self.assertEqual(result.stdout.count('@external'),1)
            again=subprocess.run([normalizer,str(normalized),LIB],capture_output=True,text=True,timeout=30)
            self.assertEqual(again.returncode,0,again.stderr)
            self.assertEqual(again.stdout,result.stdout)
            run=subprocess.run([CIDER,str(normalized),'-l',LIB],capture_output=True,timeout=30)
            self.assertEqual(run.returncode,0,run.stderr)
            dump=root/'output.dump';dump.write_bytes(run.stdout)
            converted=subprocess.run([CONVERTER,str(dump),'-t','json'],capture_output=True,text=True,timeout=30)
            self.assertEqual(converted.returncode,0,converted.stderr)
            self.assertEqual(json.loads(converted.stdout)['out'],[7])
            bad=root/'bad.futil';bad.write_text('''import "primitives/core.futil";
component child() -> () { cells { @external bad = std_reg(64); } wires { } control { } }
component main() -> () { cells { sub = child(); } wires { } control { invoke sub()(); } }
''')
            rejected=subprocess.run([normalizer,str(bad),LIB],capture_output=True,text=True,timeout=30)
            self.assertNotEqual(rejected.returncode,0)
            self.assertIn('unsupported private external cell',rejected.stderr)

    def test_export_selected_core_mismatch_is_rejected(self):
        script=Path(__file__).resolve().parents[3]/'artifacts/task6/full-model-numerical-2026-10-24/prepare_calyx_probe.py'
        if not script.is_file():self.skipTest('requires retained goal study')
        spec=importlib.util.spec_from_file_location('prepare_calyx_probe',script)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        self.assertTrue(hasattr(module,'verify_export_core'),'core/export guard missing')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'controller/memory-contracts/calyx.mlirbc'
            source.parent.mkdir(parents=True);source.write_bytes(b'frozen source')
            profile=root/'profile.json';profile.write_text('{}')
            stage={'argv':['translate',str(source)],'input_hashes':{str(source):module.digest(source),str(profile):module.digest(profile)}}
            module.verify_export_core(root,stage)
            stage['argv'][1]=str(root/'other-core.mlirbc')
            with self.assertRaises(ValueError):module.verify_export_core(root,stage)
