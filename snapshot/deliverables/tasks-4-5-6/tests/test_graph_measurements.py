"""Native measurements include children and retain failures without acceptance."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest

from compiler.stages import digest


class NativeMeasurementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / 'input.mlir'
        self.input.write_text('authenticated input')
        module = importlib.util.find_spec('compiler.measurements')
        self.assertIsNotNone(module, 'process-tree measurement implementation missing')
        self.measure = importlib.import_module('compiler.measurements').measure_stage

    def run_command(self, code, *, timeout=3, output='measurement'):
        return self.measure(
            [sys.executable, '-c', code], self.root / output,
            input_hashes={str(self.input): digest(self.input)},
            tool_hashes={sys.executable: digest(Path(sys.executable))},
            profile_sha256='a' * 64, timeout_seconds=timeout,
            sample_interval_seconds=0.01)

    def test_peak_memory_includes_concurrent_children(self):
        child = ("import mmap,sys,time; n=int(sys.argv[1])*1024*1024; "
                 "buffer=mmap.mmap(-1,n); "
                 "[(buffer.__setitem__(i,1)) for i in range(0,n,4096)]; "
                 "print('allocated',flush=True); time.sleep(0.3)")
        code = ("import subprocess,sys; "
                f"children=[subprocess.Popen([sys.executable,'-c',{child!r},str(n)]) for n in (40,60)]; "
                "[p.wait() for p in children]")
        report = self.run_command(code)
        self.assertEqual(report['status'], 'completed')
        # The parent and largest individual child cannot reach 100 MiB.
        # Both touched mappings must be included during their overlap.
        self.assertGreaterEqual(report['peak_memory_kib'], 100 * 1024)
        self.assertGreaterEqual(report['peak_processes'], 3)
        self.assertGreater(report['memory_samples'], 0)
        samples = [json.loads(line) for line in
                   (self.root / 'measurement/memory-samples.jsonl').read_text().splitlines()]
        self.assertTrue(any(sample['rss_kib'] >= 100 * 1024 for sample in samples))

    def test_failed_process_retains_raw_bytes_and_terminal_status(self):
        report = self.run_command("import os,sys;os.write(1,b'\\xff\\x00');os.write(2,b'\\xfe');sys.exit(7)")
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['exit_code'], 7)
        self.assertIsNone(report['signal'])
        self.assertEqual((self.root / 'measurement/stdout.log').read_bytes(), b'\xff\x00')
        self.assertEqual((self.root / 'measurement/stderr.log').read_bytes(), b'\xfe')
        retained = json.loads((self.root / 'measurement/measurement.json').read_text())
        self.assertEqual(retained['exit_code'], 7)
        self.assertEqual(retained['profile_sha256'], 'a' * 64)
        self.assertGreater(retained['elapsed_seconds'], 0)
        self.assertFalse((self.root / 'measurement/success.json').exists())

    def test_signal_exit_is_recorded_as_signal(self):
        report = self.run_command('import os,signal;os.kill(os.getpid(),signal.SIGTERM)')
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['exit_code'], -signal.SIGTERM)
        self.assertEqual(report['signal'], signal.SIGTERM)

    def test_timeout_terminates_child_writers_and_retains_observations(self):
        child = ("from pathlib import Path;import os,time; "
                 "Path('child.pid').write_text(str(os.getpid())); "
                 "f=open('heartbeat','ab',buffering=0); "
                 "exec('while True:\\n f.write(b\"x\");time.sleep(0.01)')")
        code = f"import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',{child!r}]);time.sleep(30)"
        try:
            report = self.run_command(code, timeout=0.4)
            self.assertEqual(report['status'], 'timeout')
            self.assertEqual(report['signal'], signal.SIGKILL)
            heartbeat = self.root / 'measurement/heartbeat'
            size = heartbeat.stat().st_size
            time.sleep(0.08)
            self.assertEqual(heartbeat.stat().st_size, size, 'timed-out child remains active')
            self.assertTrue((self.root / 'measurement/measurement.json').exists())
        finally:
            pid_file = self.root / 'measurement/child.pid'
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_changed_input_prevents_native_execution(self):
        identities = {str(self.input): digest(self.input)}
        self.input.write_text('changed input')
        report = self.measure(
            [sys.executable, '-c', "from pathlib import Path;Path('executed').touch()"],
            self.root / 'measurement', input_hashes=identities,
            tool_hashes={sys.executable: digest(Path(sys.executable))},
            profile_sha256='a' * 64, timeout_seconds=1)
        self.assertEqual(report['status'], 'dependency-error')
        self.assertIsNone(report['exit_code'])
        self.assertFalse((self.root / 'measurement/executed').exists())

    def test_input_changed_during_execution_cannot_be_labeled_completed(self):
        code = f"from pathlib import Path;Path({str(self.input)!r}).write_text('changed')"
        report = self.run_command(code)
        self.assertEqual(report['exit_code'], 0)
        self.assertEqual(report['status'], 'dependency-error')
        self.assertFalse(report['inputs_authenticated'])

    def test_previous_evidence_is_never_overwritten(self):
        first = self.run_command('print("original")')
        with self.assertRaisesRegex(ValueError, 'fresh'):
            self.run_command('print("replacement")')
        retained = json.loads((self.root / 'measurement/measurement.json').read_text())
        self.assertEqual(retained, first)
        self.assertEqual((self.root / 'measurement/stdout.log').read_text(), 'original\n')

    def test_conflicting_input_and_tool_identities_cannot_be_overridden(self):
        with self.assertRaisesRegex(ValueError, 'conflicting'):
            self.measure(
                [sys.executable, '-c', "from pathlib import Path;Path('executed').touch()"],
                self.root / 'measurement',
                input_hashes={str(self.input): digest(self.input), sys.executable: '0' * 64},
                tool_hashes={sys.executable: digest(Path(sys.executable))},
                profile_sha256='a' * 64, timeout_seconds=1)
        self.assertFalse((self.root / 'measurement/executed').exists())

    def test_collector_source_identity_is_also_authenticated(self):
        source = self.measure.__code__.co_filename
        report = self.measure(
            [sys.executable, '-c', "from pathlib import Path;Path('executed').touch()"],
            self.root / 'measurement',
            input_hashes={str(self.input): digest(self.input), source: '0' * 64},
            tool_hashes={sys.executable: digest(Path(sys.executable))},
            profile_sha256='a' * 64, timeout_seconds=1)
        self.assertEqual(report['status'], 'dependency-error')
        self.assertFalse((self.root / 'measurement/executed').exists())


if __name__ == '__main__':
    unittest.main()
