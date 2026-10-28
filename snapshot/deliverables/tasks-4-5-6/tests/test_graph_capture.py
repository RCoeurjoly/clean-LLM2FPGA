import json
import tempfile
from pathlib import Path
import unittest
import torch


class AddBuffer(torch.nn.Module):
    def __init__(self, value=1):
        super().__init__()
        self.register_buffer('offset', torch.tensor([value], dtype=torch.int64))

    def forward(self, values):
        return values + self.offset


class Unsupported(torch.nn.Module):
    def forward(self, values):
        return torch.sin(values)


class Matmul(torch.nn.Module):
    def forward(self, left, right):
        return torch.matmul(left, right)


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)

    def test_token_ids_remain_runtime_inputs(self):
        from compiler.frontend.capture import capture_model
        receipt = capture_model(AddBuffer(), (torch.tensor([2], dtype=torch.int64),), self.output)
        exported = torch.export.load(self.output / 'model.pt2').module()
        self.assertTrue(torch.equal(exported(torch.tensor([9])), torch.tensor([10])))
        self.assertEqual(receipt.user_input_count, 1)

    def test_changed_buffers_and_graph_change_identity(self):
        from compiler.frontend.capture import capture_model
        args = (torch.tensor([2], dtype=torch.int64),)
        first = capture_model(AddBuffer(1), args, self.output / 'first')
        second = capture_model(AddBuffer(2), args, self.output / 'second')
        third = capture_model(torch.nn.Identity(), args, self.output / 'third')
        self.assertNotEqual(first.program_sha256, second.program_sha256)
        self.assertNotEqual(first.program_sha256, third.program_sha256)
        fourth = capture_model(AddBuffer(1), (torch.tensor([8], dtype=torch.int64),), self.output / 'fourth')
        self.assertEqual(first.program_sha256, fourth.program_sha256)

    def test_unsupported_operator_reports_node_types_location_stage(self):
        from compiler.frontend.capture import capture_model
        with self.assertRaisesRegex(ValueError, 'capture.*aten.sin.default.*dtype=.*location='):
            capture_model(Unsupported(), (torch.tensor([1.0]),), self.output)
        self.assertFalse((self.output / 'capture.json').exists())

    def test_task2_matmul_shape_retains_two_integer_runtime_inputs(self):
        from compiler.frontend.capture import capture_model
        args = (torch.arange(1, 17, dtype=torch.int32),
                torch.arange(16, 0, -1, dtype=torch.int32))
        receipt = capture_model(Matmul(), args, self.output)
        self.assertEqual(receipt.user_input_count, 2)
        self.assertEqual(receipt.operators, {'aten.matmul.default': 1})
        changed = (args[0] * -3, args[1] + 7)
        exported = torch.export.load(self.output / 'model.pt2').module()
        self.assertTrue(torch.equal(exported(*changed), Matmul()(*changed)))
        self.assertEqual(exported(*changed).dtype, torch.int32)

    def test_raw_ir_serialization_retains_pytorch_source_locations(self):
        from compiler.frontend.capture import import_raw_torch, serialize_raw_torch
        exported = torch.export.export(AddBuffer(), (torch.tensor([2], dtype=torch.int64),))
        module = import_raw_torch(exported)
        serialized = serialize_raw_torch(module)
        self.assertIn(str(Path(__file__).resolve()), serialized)
        self.assertIn('loc(', serialized)
        from torch_mlir.ir import Module
        with module.context:
            restored = Module.parse(serialized)
            self.assertTrue(restored.operation.verify())

    def test_graph_input_manifest_preserves_order_and_excludes_runtime_values(self):
        from compiler.frontend.capture import capture_model
        receipt = capture_model(AddBuffer(7), (torch.tensor([99], dtype=torch.int64),), self.output)
        manifest = json.loads((self.output / 'graph-inputs.json').read_text())
        self.assertEqual(manifest['signature_sha256'], receipt.signature_sha256)
        self.assertEqual([item['kind'] for item in manifest['inputs']], ['BUFFER', 'USER_INPUT'])
        self.assertEqual([item['index'] for item in manifest['inputs']], [0, 1])
        self.assertEqual(manifest['inputs'][0]['target'], 'offset')
        self.assertEqual(manifest['inputs'][0]['sha256'], receipt.state_identities['offset']['sha256'])
        self.assertIsNone(manifest['inputs'][1]['sha256'])
        changed = self.output / 'changed'
        capture_model(AddBuffer(7), (torch.tensor([-123], dtype=torch.int64),), changed)
        self.assertEqual(manifest, json.loads((changed / 'graph-inputs.json').read_text()))
