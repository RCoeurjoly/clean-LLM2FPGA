"""Large compiler artifacts must not be copied into RAM just to hash them."""
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from compiler.stages import digest


class ArtifactDigestTests(unittest.TestCase):
    def test_large_artifact_hash_has_bounded_memory_and_exact_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'artifact.mlir'
            block = b'x' * (1024 * 1024)
            with path.open('wb') as output:
                for _ in range(32):
                    output.write(block)
            tracemalloc.start()
            try:
                identity = digest(path)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual(identity,
                '05f052c8f6da8ee5228ec291820b559c4be183773b9e97a6b82e30dacff85dd3')
            self.assertLess(peak, 1024 * 1024,
                            'artifact hashing must use bounded buffers')
