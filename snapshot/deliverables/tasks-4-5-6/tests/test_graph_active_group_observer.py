"""Read-only active-group diagnostics must report only declared one-bit wires."""
import importlib.util
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

EVIDENCE=Path(__file__).resolve().parents[3]/'artifacts/task6/full-model-numerical-2026-10-24'


@unittest.skipUnless(EVIDENCE.is_dir(),'requires retained goal study')
class ActiveGroupObserverTests(unittest.TestCase):
    def test_compiled_reader_reports_active_groups_without_changing_state(self):
        source=EVIDENCE/'active_groups.py'
        self.assertTrue(source.is_file(),'active-group reader missing')
        spec=importlib.util.spec_from_file_location('active_groups',source)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        prefix='tb__DOT__dut__DOT__main_1_instance__DOT__'
        fields=[prefix+'bb0_7_go__DOT__in',prefix+'generation_ctx_4_main_instance__DOT__bb0_27_go__DOT__in',
                prefix+'generation_ctx_5_main_instance__DOT__bb0_9_go__DOT__in']
        header='\n'.join(' CData/*0:0*/ '+name+';' for name in fields)
        header+='\n CData/*0:0*/ unrelated_go__DOT__in;\n IData/*31:0*/ '+prefix+'wide_go__DOT__in;\n'
        cpp,mapping=module.render(header)
        self.assertEqual(set(mapping),set(fields))
        with self.assertRaises(ValueError):module.render('CData/*0:0*/ unrelated;')
        with self.assertRaises(ValueError):module.render(header+'\n CData/*0:0*/ '+fields[0]+';')
        cc=shutil.which('c++');self.assertIsNotNone(cc)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            declarations='\n'.join('bool '+name+';' for name in fields)
            fixture='#include <cstring>\nstruct Vtb___024root { '+declarations+' };\n'+cpp+'''
int main() {
 Vtb___024root state; std::memset(&state,0,sizeof(state));
 state.'''+fields[0]+'''=true;
 state.'''+fields[1]+'''=true;
 Vtb___024root before=state;
 report_active_groups(state);
 return std::memcmp(&state,&before,sizeof(state)) ? 7 : 0;
}
'''
            (root/'test.cpp').write_text(fixture)
            built=subprocess.run([cc,'-std=c++20',str(root/'test.cpp'),'-o',str(root/'test')],capture_output=True,text=True,timeout=30)
            self.assertEqual(built.returncode,0,built.stderr)
            run=subprocess.run([str(root/'test')],capture_output=True,text=True,timeout=30)
            self.assertEqual(run.returncode,0,run.stderr)
            self.assertEqual(set(run.stderr.split()),{'group=controller:bb0_7','group=ctx4:bb0_27'})
