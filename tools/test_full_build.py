"""Reject invalid assembly inputs and decoded configurations."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import build
from tools.verify_decoded_frames import verify_frames


class AssemblyTest(unittest.TestCase):
    def test_wrong_fasm_fails_before_conversion(self):
        with tempfile.TemporaryDirectory() as tmp:
            route = Path(tmp) / 'route.fasm'
            route.write_text('wrong route')
            with patch('build.subprocess.run') as run:
                with self.assertRaises(ValueError):
                    build.assemble(str(route), 'unused-db')
                run.assert_not_called()

    def test_wrong_final_bitstream_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            bit = Path(tmp) / 'model.bit'
            bit.write_bytes(b'incorrect bitstream')
            with self.assertRaises(ValueError):
                build.require_hash(bit, build.EXPECTED)

    def test_decoded_frames_match_and_reject_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            frames = Path(tmp) / 'model.frm'
            decoded = Path(tmp) / 'decoded'
            words = [1] + [0] * 100
            frames.write_text('0x1 ' + ','.join(f'{w:08x}' for w in words))
            valid = '.frame 0x1\n' + ' '.join(f'{w:08x}' for w in words)
            decoded.write_text(valid)
            verify_frames(frames, decoded)
            for invalid in ('', valid.replace('00000001', '00000002'),
                            valid + '\n.frame 0x2\n' + ' '.join(['00000001'] * 101),
                            valid[:-9]):
                decoded.write_text(invalid)
                with self.assertRaises(ValueError):
                    verify_frames(frames, decoded)


if __name__ == '__main__':
    unittest.main()
