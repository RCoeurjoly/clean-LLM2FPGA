"""Exercise the build runner without invoking model compilation or routing."""
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



if __name__ == '__main__':
    unittest.main()
