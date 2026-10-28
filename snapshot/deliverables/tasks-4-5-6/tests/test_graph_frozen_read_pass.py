"""Native frozen-load narrowing requires authenticated values and readonly uses."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from compiler.frozen_ranges import build_frozen_range_certificate

TOOL = '/nix/store/naaffl6nqhpa8q0k93j3qj6qjwlqwwwk-llm2fpga-typed-torch-driver-0.1.0/bin/llm2fpga-torch-opt'


@unittest.skipUnless(os.environ.get('LLM2FPGA_FROZEN_RANGE_PLUGIN'), 'experimental native plugin not selected')
class FrozenReadPassTests(unittest.TestCase):
    def invoke(self, body, *, poison=False, other_argument=False, declarations='', values=(-128, 127)):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = b''.join((v % (1 << 64)).to_bytes(8, 'little') for v in values)
            identity = hashlib.sha256(data).hexdigest()
            manifest = root / 'inputs.json'
            manifest.write_text(json.dumps({'schema_version': 1, 'inputs': [{
                'index': 0, 'name': 'weight', 'kind': 'PARAMETER', 'target': 'tensor',
                'dtype': 'torch.int64', 'shape': [2], 'sha256': identity}]}))
            image = root / 'image.hex'
            image.write_text(''.join(f'{v % (1 << 64):016x}\n' for v in values))
            certificate = root / 'ranges.json'
            build_frozen_range_certificate(manifest, {'tensor': image}, certificate)
            if poison:
                image.write_text('0000000000000000\n0000000000000000\n')
            source = '''module { %s func.func @kernel(%%data: memref<2xi64> {
                llm2fpga.input_origin = {kind = "PARAMETER", target = "tensor", sha256 = "%s"}
              }%s) -> i64 { %%zero = arith.constant 0 : index
              %s
            }}''' % (declarations, identity, ', %other: memref<2xi64>' if other_argument else '', body)
            command = [os.environ.get('LLM2FPGA_TORCH_PASS_TOOL', TOOL),
                '--load-pass-plugin=' + os.environ['LLM2FPGA_FROZEN_RANGE_PLUGIN'],
                '--verify-each', '--pass-pipeline=builtin.module(llm2fpga-narrow-frozen-reads{certificate=' +
                str(certificate) + ' certificate-sha256=' + hashlib.sha256(certificate.read_bytes()).hexdigest() + '})']
            return subprocess.run(command, input=source, text=True, capture_output=True, timeout=30)

    def test_readonly_load_gets_signed_narrowing(self):
        result = self.invoke('%v = memref.load %data[%zero] : memref<2xi64> return %v : i64')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('arith.trunci', result.stdout)
        self.assertIn('i64 to i8', result.stdout)
        self.assertIn('arith.extsi', result.stdout)

    def test_changed_image_is_rejected(self):
        result = self.invoke('%v = memref.load %data[%zero] : memref<2xi64> return %v : i64', poison=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('frozen read range', result.stderr)

    def test_positive_byte_values_use_unsigned_extension(self):
        result = self.invoke('%v = memref.load %data[%zero] : memref<2xi64> return %v : i64', values=(128, 255))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('arith.extui', result.stdout)
        self.assertNotIn('arith.extsi', result.stdout)

    def test_write_through_alias_is_rejected(self):
        result = self.invoke('''%alias = memref.cast %data : memref<2xi64> to memref<?xi64>
            %v = memref.load %data[%zero] : memref<2xi64>
            memref.store %v, %alias[%zero] : memref<?xi64>
            return %v : i64''')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('readonly', result.stderr)

    def test_other_argument_cannot_be_assumed_disjoint(self):
        result = self.invoke('''%changed = arith.constant 300 : i64
            memref.store %changed, %other[%zero] : memref<2xi64>
            %v = memref.load %data[%zero] : memref<2xi64> return %v : i64''', other_argument=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('readonly', result.stderr)

    def test_external_call_without_arguments_has_unknown_effects(self):
        result = self.invoke('''func.call @external() : () -> ()
            %v = memref.load %data[%zero] : memref<2xi64> return %v : i64''',
            declarations='func.func private @external()')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('readonly', result.stderr)

    def test_store_to_private_allocation_is_disjoint(self):
        result = self.invoke('''%private = memref.alloc() : memref<2xi64>
            %changed = arith.constant 300 : i64
            memref.store %changed, %private[%zero] : memref<2xi64>
            %v = memref.load %data[%zero] : memref<2xi64> return %v : i64''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('arith.trunci', result.stdout)
