"""Native graph-input annotation checks typed argument geometry first."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SOURCE = '''module {
 func.func @main(%a: !torch.vtensor<[1],si64>, %b: !torch.vtensor<[1],si64>) -> !torch.vtensor<[1],si64> {
  return %b : !torch.vtensor<[1],si64>
 }
}'''


class InputOriginTests(unittest.TestCase):
    def annotate(self, shape=None, manifest_hash=None):
        tool = os.environ['LLM2FPGA_TORCH_PASS_TOOL']
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'inputs.json'
            inputs = [dict(index=0, name='b_weights', kind='BUFFER', target='weights',
                shape=shape or [1], dtype='torch.int64', sha256='1' * 64),
                dict(index=1, name='values', kind='USER_INPUT', target=None,
                     shape=[1], dtype='torch.int64', sha256=None)]
            path.write_text(json.dumps(dict(schema_version=1, signature_sha256='2' * 64,
                                            inputs=inputs)))
            identity = manifest_hash or hashlib.sha256(path.read_bytes()).hexdigest()
            arguments = [tool, '--verify-each',
                '--llm2fpga-annotate-graph-inputs=manifest=' + str(path) +
                ' manifest-sha256=' + identity]
            result = subprocess.run(arguments, input=SOURCE,
                text=True, capture_output=True, timeout=30)
            return result


    def test_shape_mismatch_is_rejected(self):
        result = self.annotate(shape=[2])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('argument geometry', result.stderr)

    def test_manifest_identity_is_required(self):
        result = self.annotate(manifest_hash='0' * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('manifest identity', result.stderr)


if __name__ == '__main__':
    unittest.main()
