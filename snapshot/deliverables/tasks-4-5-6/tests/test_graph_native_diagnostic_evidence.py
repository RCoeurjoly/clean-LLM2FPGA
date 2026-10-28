"""Failed native diagnostics must not leave stale acceptance or lose provenance."""
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

EVIDENCE=Path(__file__).resolve().parents[3]/'artifacts/task6/full-model-numerical-2026-10-24'


def load(name):
    spec=importlib.util.spec_from_file_location('_native_diagnostic_test',EVIDENCE/name)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


@unittest.skipUnless(EVIDENCE.is_dir(),'requires retained goal study')
class NativeDiagnosticEvidenceTests(unittest.TestCase):
    def test_temporary_runtime_source_survives_process_cleanup(self):
        module=load('replay_native_ir.py')
        self.assertTrue(hasattr(module,'retain_runtime_source'),'ephemeral source retention missing')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'ephemeral.py';source.write_bytes(b'generated = 7\n')
            retained=module.retain_runtime_source(source,root/'retained')
            source.unlink()
            self.assertTrue(retained.is_file())
            module.verify_retained_files({str(retained):hashlib.sha256(b'generated = 7\n').hexdigest()})

    def test_missing_execution_environment_invalidates_previous_report(self):
        for script,relative in (('replay_native_ir.py','native-ir/comparison.json'),
                                ('probe_cpu_abi.py','cpu-abi-probe/result.json')):
            with self.subTest(script=script),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);shutil.copyfile(EVIDENCE/script,root/script)
                report=root/relative;report.parent.mkdir();report.write_text('{"old_success":true}')
                env={**os.environ,'PYTHONPATH':''}
                result=subprocess.run([sys.executable,'-S',str(root/script)],env=env,
                    capture_output=True,text=True,timeout=30)
                self.assertNotEqual(result.returncode,0)
                self.assertFalse(report.exists(),'failed preflight retained old acceptance')

    def test_reference_metadata_and_jit_runtime_changes_are_rejected(self):
        module=load('replay_native_ir.py')
        self.assertTrue(hasattr(module,'verify_retained_files'),'runtime provenance guard missing')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);paths=[root/'reference-identities.json',root/'jit-runtime.so']
            for p in paths:p.write_bytes(b'original identity\n')
            identities={str(p):hashlib.sha256(b'original identity\n').hexdigest() for p in paths}
            module.verify_retained_files(identities)
            for p in paths:
                p.write_bytes(b'changed identity\n')
                with self.assertRaises(ValueError):module.verify_retained_files(identities)
                p.write_bytes(b'original identity\n')
            paths[0].unlink()
            with self.assertRaises(ValueError):module.verify_retained_files(identities)

    def test_optimized_python_cannot_disable_abi_result_checks(self):
        source=EVIDENCE/'probe_cpu_abi.py'
        for actual,expected,observed in ((42,7,[(0,7)]),(7,7,[])):
            code=f'''import importlib.util,sys
s=importlib.util.spec_from_file_location('probe',{str(source)!r});m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
try:m.check_probe_result({actual!r},{expected!r},{observed!r})
except ValueError:sys.exit(7)
sys.exit(0)
'''
            result=subprocess.run([sys.executable,'-O','-c',code],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,7,result.stdout+result.stderr)
