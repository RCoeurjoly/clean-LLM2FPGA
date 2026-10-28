"""Verification runtime inputs must match compilation, and outputs be complete."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

HELPER = Path(__file__).resolve().parents[1] / 'scripts/comparison/graph_rtl_runtime.py'

class RuntimeVerificationTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('graph_rtl_runtime', HELPER)
        self.runtime = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runtime)

    def binding(self, name='weights.hex', content=b'0001\n'):
        return {'init_file': name, 'init_sha256': hashlib.sha256(content).hexdigest()}

    def test_frozen_images_are_authenticated_before_installing(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work); source = root/'source'; target = root/'target'
            source.mkdir(); (source/'weights.hex').write_bytes(b'0001\n')
            result = self.runtime.install_frozen_images([self.binding()], source, target)
            self.assertEqual((target/'weights.hex').read_bytes(), b'0001\n')
            self.assertEqual(result, {'weights.hex': self.binding()['init_sha256']})

    def test_changed_or_missing_image_leaves_no_partial_installation(self):
        for changed in (False, True):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as work:
                root=Path(work); (root/'good.hex').write_bytes(b'0001\n')
                if changed: (root/'weights.hex').write_bytes(b'0002\n')
                with self.assertRaises((ValueError, FileNotFoundError)):
                    self.runtime.install_frozen_images([self.binding('good.hex'),self.binding()],root,root/'target')
                self.assertFalse((root/'target/good.hex').exists())

    def test_unsafe_image_paths_are_rejected(self):
        for name in ('../weights.hex','/tmp/weights.hex','a/../../weights.hex'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as work:
                with self.assertRaises(ValueError):
                    self.runtime.install_frozen_images([self.binding(name)],Path(work),Path(work)/'target')

    def test_conflicting_duplicate_image_is_rejected(self):
        with tempfile.TemporaryDirectory() as work:
            root=Path(work); (root/'weights.hex').write_bytes(b'0001\n')
            with self.assertRaises(ValueError):
                self.runtime.install_frozen_images([self.binding(),self.binding(content=b'0002\n')],root,root/'target')

    def test_complete_ordered_output_is_decoded(self):
        self.assertEqual(self.runtime.decode_output('RESULT 0 00\nRESULT 1 ff\n',2,8),[0,255])

    def test_missing_reordered_duplicate_and_out_of_width_results_fail(self):
        for stdout in ('RESULT 0 00\n','RESULT 1 ff\nRESULT 0 00\n','RESULT 0 00\nRESULT 0 ff\n','RESULT 0 100\nRESULT 1 ff\n'):
            with self.subTest(stdout=stdout), self.assertRaises(ValueError):
                self.runtime.decode_output(stdout,2,8)

    def test_generation_streams_are_complete_and_independently_decoded(self):
        stdout = 'TOKEN 0 01\nLOGIT 0 ff\nTOKEN 1 02\nLOGIT 1 00\n'
        self.assertEqual(self.runtime.decode_output(stdout, 2, 8, prefix='TOKEN'), [1, 2])
        self.assertEqual(self.runtime.decode_output(stdout, 2, 8, prefix='LOGIT'), [255, 0])
        for prefix in ('TOKEN', 'LOGIT'):
            for suffix in ('', 'garbage', '0 03', '2 03'):
                malformed = stdout + prefix + ' ' + suffix + '\n'
                with self.subTest(prefix=prefix, suffix=suffix), self.assertRaises(ValueError):
                    self.runtime.decode_output(malformed, 2, 8, prefix=prefix)

    def test_missing_memory_warning_invalidates_otherwise_complete_result(self):
        with self.assertRaises(ValueError):
            self.runtime.decode_output('%Warning: weights.hex:0: $readmem file not found\nRESULT 0 00\n',1,8)

class RuntimeCliTests(unittest.TestCase):
    def test_cli_exposes_isolated_runtime_directory(self):
        result = subprocess.run([sys.executable,
            str(HELPER.with_name('verify_graph_block_rtl.py')), '--help'],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--work-dir', result.stdout)

    def test_missing_binary_removes_stale_receipt_before_reference_loading(self):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work); receipt = root/'receipt.json'
            receipt.write_text('{"passed":true}')
            command = [sys.executable, str(HELPER.with_name('verify_graph_block_rtl.py'))]
            for name in ('root','binary','snapshot','package','reference-source'):
                command.extend(['--'+name, str(root/name)])
            command.extend(['--output', str(receipt)])
            result = subprocess.run(command,capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('Compile hardware before', result.stderr)
            self.assertFalse(receipt.exists())

if __name__ == '__main__': unittest.main()
