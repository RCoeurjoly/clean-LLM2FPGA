"""Images originate only from frozen tensors in the captured graph signature."""
import hashlib
from pathlib import Path
import tempfile
import unittest
import torch


class Buffered(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.register_buffer('weights', torch.tensor([-3, 5, 2], dtype=torch.int64))
    def forward(self, values):
        return values + self.weights


class GraphImageTests(unittest.TestCase):
    def test_frozen_buffer_words_and_runtime_exclusion(self):
        from compiler.graph_images import materialize_graph_images
        exported = torch.export.export(Buffered(), (torch.ones(3, dtype=torch.int64),))
        tensor = exported.state_dict['weights']
        inputs = {'schema_version': 1, 'inputs': [dict(index=0, kind='BUFFER', target='weights',
            shape=[3], dtype='torch.int64', sha256=hashlib.sha256(tensor.view(torch.uint8).numpy().tobytes()).hexdigest()),
            dict(index=1, kind='USER_INPUT', target=None, shape=[3], dtype='torch.int64', sha256=None)]}
        with tempfile.TemporaryDirectory() as temporary:
            result = materialize_graph_images(exported, inputs, Path(temporary))
            self.assertEqual(set(result), {'weights'})
            self.assertEqual(result['weights'].read_text(),
                'fffffffffffffffd\n0000000000000005\n0000000000000002\n')
            inputs['inputs'][0]['sha256'] = '0' * 64
            with self.assertRaisesRegex(ValueError, 'tensor identity'):
                materialize_graph_images(exported, inputs, Path(temporary))


if __name__ == '__main__':
    unittest.main()
