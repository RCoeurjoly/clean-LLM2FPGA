"""Exercise the build runner without invoking model compilation or routing."""
import contextlib
import io
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import build


class StageTest(unittest.TestCase):
    def test_missing_input_stops_before_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            spec = {'argv': [sys.executable, '-c', 'raise AssertionError()'],
                    'inputs': [str(Path(tmp) / 'missing')], 'expected_outputs': {}}
            with self.assertRaises(FileNotFoundError):
                build.run_stage(spec, out)
            self.assertFalse(out.exists())

    def test_stage_consumes_declared_input_and_records_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'input'
            source.write_text('generated model')
            out = root / 'out'
            spec = {'argv': [sys.executable, '-c',
                            'from pathlib import Path; import sys; '
                            'Path(sys.argv[2]).write_bytes(Path(sys.argv[1]).read_bytes())',
                            str(source), '@OUTPUT@/result'],
                    'inputs': [str(source)], 'expected_outputs': {'result': '0' * 64},
                    'timeout_seconds': 10, 'environment': {}}
            build.run_stage(spec, out)
            report = json.loads((out / 'build-result.json').read_text())
            self.assertEqual((out / 'result').read_text(), 'generated model')
            self.assertEqual(report['outputs']['result'], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(report['historical_matches']['result'], False)

    def test_missing_output_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = {'argv': [sys.executable, '-c', 'pass'], 'inputs': [],
                    'expected_outputs': {'missing': '0' * 64}, 'timeout_seconds': 10}
            with self.assertRaises(FileNotFoundError):
                build.run_stage(spec, Path(tmp) / 'out')

    def test_failed_command_reports_compiler_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = {'argv': [sys.executable, '-c',
                            'from pathlib import Path; '
                            'Path("native.log").write_text("native router diagnostic"); '
                            'print("compiler diagnostic", flush=True); raise SystemExit(1)'],
                    'inputs': [], 'expected_outputs': {}, 'timeout_seconds': 10}
            diagnostic = io.StringIO()
            with contextlib.redirect_stderr(diagnostic):
                with self.assertRaises(build.subprocess.CalledProcessError):
                    build.run_stage(spec, Path(tmp) / 'out')
            self.assertIn('compiler diagnostic', diagnostic.getvalue())
            self.assertIn('native router diagnostic', diagnostic.getvalue())

    def test_wrong_fasm_fails_before_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            route = Path(tmp) / 'route.fasm'
            route.write_text('wrong route')
            with patch('build.subprocess.run') as run:
                with self.assertRaises(ValueError):
                    build.assemble({'fasm': str(route)}, Path(tmp) / 'out')
                run.assert_not_called()

    def test_wrong_final_bitstream_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bit = Path(tmp) / 'model.bit'
            bit.write_bytes(b'incorrect bitstream')
            with self.assertRaises(ValueError):
                build.require_hash(bit, build.EXPECTED)


class PortabilityTest(unittest.TestCase):
    def test_build_inputs_have_no_machine_specific_paths(self):
        import re
        root = Path(__file__).resolve().parents[1]
        files = [root / 'flake.nix', root / 'pipeline/flow.json']
        files += list((root / 'nix').glob('*.nix'))
        files += list((root / 'source/toolchain/tests').glob('*.py'))
        for path in files:
            with self.subTest(path=str(path.relative_to(root))):
                self.assertIsNone(re.search(r'/nix/store/[a-z0-9]{32}-|/home/roland/|/usr/bin/(?!env)',
                                            path.read_text()))


if __name__ == '__main__':
    unittest.main()
