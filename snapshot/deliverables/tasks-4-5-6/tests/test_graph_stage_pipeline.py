"""Stage receipts cannot turn compiler failure into acceptance."""
import hashlib
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class StageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.input = self.root / 'input.mlir'
        self.input.write_text('module {}')
        self.output = self.root / 'stage'

    def spec(self, code, outputs=('result.mlir',)):
        from compiler.stages import StageSpec
        return StageSpec(name='lower', argv=(sys.executable, '-c', code),
                         input_hashes={str(self.input): digest(self.input)},
                         tool_hashes={sys.executable: digest(sys.executable)},
                         pass_identities={'pipeline': 'test-pass-v1'}, outputs=outputs,
                         timeout_seconds=2)

    def test_failed_stage_removes_previous_success_and_keeps_diagnostics(self):
        from compiler.stages import run_stage, StageError
        success = self.spec("from pathlib import Path; Path('result.mlir').write_text('first')")
        run_stage(success, self.output)
        failure = self.spec("import sys; print('unsupported scf op', file=sys.stderr); sys.exit(7)")
        with self.assertRaises(StageError) as raised:
            run_stage(failure, self.output)
        self.assertEqual(raised.exception.receipt.exit_code, 7)
        self.assertFalse((self.output / 'success.json').exists())
        self.assertFalse((self.output / 'result.mlir').exists())
        self.assertIn('unsupported scf op', (self.output / 'stderr.log').read_text())
        self.assertTrue((self.output / 'failure.json').exists())

    def test_exit_zero_without_declared_output_is_failure(self):
        from compiler.stages import run_stage, StageError
        with self.assertRaisesRegex(StageError, 'missing declared output'):
            run_stage(self.spec('pass'), self.output)
        self.assertFalse((self.output / 'success.json').exists())

    def test_altered_input_or_tool_fails_before_execution(self):
        from compiler.stages import run_stage, StageError
        spec = self.spec("raise AssertionError('must not execute')")
        self.input.write_text('changed input')
        with self.assertRaisesRegex(StageError, 'input identity mismatch'):
            run_stage(spec, self.output)
        self.assertFalse((self.output / 'success.json').exists())
        wrong_tool = replace(spec, input_hashes={}, tool_hashes={sys.executable: '0' * 64})
        with self.assertRaisesRegex(StageError, 'tool identity mismatch'):
            run_stage(wrong_tool, self.output)

    def test_input_changed_during_execution_is_not_accepted(self):
        from compiler.stages import run_stage, StageError
        code = ("from pathlib import Path; "
                f"Path({str(self.input)!r}).write_text('changed during run'); "
                "Path('result.mlir').write_text('module {}')")
        with self.assertRaisesRegex(StageError, 'input identity mismatch'):
            run_stage(self.spec(code), self.output)
        self.assertFalse((self.output / 'success.json').exists())

    def test_relative_executable_identity_is_rejected(self):
        from compiler.stages import run_stage
        spec = replace(self.spec('pass'), argv=('python', '-c', 'pass'), tool_hashes={'python': '0' * 64})
        with self.assertRaisesRegex(ValueError, 'absolute'):
            run_stage(spec, self.output)

    def test_timeout_retains_diagnostics_without_success(self):
        from compiler.stages import run_stage, StageError
        spec = replace(self.spec("import time; print('lowering started', flush=True); time.sleep(10)"),
                       timeout_seconds=0.1)
        with self.assertRaisesRegex(StageError, 'timeout'):
            run_stage(spec, self.output)
        self.assertIn('lowering started', (self.output / 'stdout.log').read_text())
        self.assertFalse((self.output / 'success.json').exists())

    def test_native_logs_are_visible_while_the_stage_is_still_running(self):
        from compiler.stages import run_stage
        from concurrent.futures import ThreadPoolExecutor
        import os
        import time
        code="""import os,time
from pathlib import Path
os.write(1,b'lowering started\\n');os.write(2,b'frontend active\\n')
Path('started').write_text(str(os.getpid()))
while not Path('release').exists():time.sleep(0.01)
Path('result.mlir').write_text('module {}')
"""
        with ThreadPoolExecutor(max_workers=1) as executor:
            future=executor.submit(run_stage,self.spec(code),self.output)
            try:
                deadline=time.monotonic()+1.5
                while not (self.output/'started').exists() and time.monotonic()<deadline:
                    time.sleep(0.01)
                self.assertTrue((self.output/'started').exists(),'native process never started')
                os.kill(int((self.output/'started').read_text()),0)
                self.assertFalse(future.done())
                self.assertTrue((self.output/'stdout.log').is_file(),'live stdout is buffered until completion')
                self.assertEqual((self.output/'stdout.log').read_bytes(),b'lowering started\n')
                self.assertEqual((self.output/'stderr.log').read_bytes(),b'frontend active\n')
                self.assertFalse((self.output/'success.json').exists())
            finally:
                (self.output/'release').touch()
            self.assertTrue(future.result(timeout=2).success)

    def test_native_logs_preserve_raw_bytes_without_text_decoding(self):
        from compiler.stages import run_stage
        code="import os;from pathlib import Path;os.write(1,bytes([255,0,13,10]));os.write(2,bytes([254,13,10]));Path('result.mlir').write_text('module {}')"
        self.assertTrue(run_stage(self.spec(code),self.output).success)
        self.assertEqual((self.output/'stdout.log').read_bytes(),bytes([255,0,13,10]))
        self.assertEqual((self.output/'stderr.log').read_bytes(),bytes([254,13,10]))

    def test_signal_exit_cannot_accept_an_output(self):
        from compiler.stages import run_stage, StageError
        import signal
        code = ("import os, signal; from pathlib import Path; "
                "Path('result.mlir').write_text('partial'); os.kill(os.getpid(), signal.SIGTERM)")
        with self.assertRaises(StageError) as raised:
            run_stage(self.spec(code), self.output)
        self.assertEqual(raised.exception.receipt.signal, signal.SIGTERM)
        self.assertEqual(raised.exception.receipt.output_hashes, {})
        self.assertFalse((self.output / 'success.json').exists())

    def test_output_paths_cannot_escape_stage(self):
        from compiler.stages import run_stage
        with self.assertRaisesRegex(ValueError, 'output path'):
            run_stage(self.spec('pass', ('../escape',)), self.output)

    def test_success_identity_excludes_elapsed_time(self):
        from compiler.stages import run_stage
        spec = self.spec("from pathlib import Path; Path('result.mlir').write_text('module {}')")
        first = run_stage(spec, self.output)
        second = run_stage(spec, self.output)
        self.assertEqual(first.identity_sha256, second.identity_sha256)
        self.assertEqual(first.output_hashes, {'result.mlir': hashlib.sha256(b'module {}').hexdigest()})
        self.assertTrue((self.output / 'observations.json').exists())

    def test_receipt_retains_auditable_stage_specification(self):
        import json
        from compiler.stages import run_stage
        spec = self.spec("from pathlib import Path; Path('result.mlir').write_text('module {}')")
        receipt = run_stage(spec, self.output)
        manifest = json.loads((self.output / 'spec.json').read_text())
        self.assertEqual(manifest['argv'], list(spec.argv))
        self.assertEqual(manifest['input_hashes'], spec.input_hashes)
        self.assertEqual(manifest['tool_hashes'], spec.tool_hashes)
        self.assertEqual(manifest['pass_identities'], spec.pass_identities)
        canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), receipt.spec_sha256)

    def test_stage_specification_is_reserved_and_cannot_be_a_compiler_output(self):
        from compiler.stages import run_stage
        with self.assertRaisesRegex(ValueError, 'invalid output path'):
            run_stage(self.spec('pass', outputs=('spec.json',)), self.output)
