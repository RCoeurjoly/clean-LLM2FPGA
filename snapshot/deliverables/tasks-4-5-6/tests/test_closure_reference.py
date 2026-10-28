"""Required closure checks use explicit inputs and package-specific bounds."""
from pathlib import Path
import importlib.util
import json
import struct
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class ClosureReferenceTest(unittest.TestCase):
    def load_gate(self):
        path = ROOT / "scripts/comparison/verify_closure_reference.py"
        self.assertTrue(path.is_file(), "portable closure reference gate is missing")
        spec = importlib.util.spec_from_file_location("closure_reference", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_missing_snapshot_is_error(self):
        gate = self.load_gate()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package"
            package.mkdir()
            with self.assertRaisesRegex(ValueError, "snapshot"):
                gate.verify_reference(package, root / "missing", ROOT / "artifacts/reference/tinystories-1m-fixed-point-continuation-oracle.json", root / "result.json")
            self.assertFalse((root / "result.json").exists())

    def test_altered_package_is_rejected(self):
        gate = self.load_gate()
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            (package / "manifest.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "package"):
                gate.authenticate_package(package)

    def test_family_width_certificate_is_package_specific(self):
        gate = self.load_gate()
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary)
            descriptor = {"logical_shape": [1, 2], "format": "symmetric_int8_per_output", "offset": 0, "nbytes": 2, "scale_offset": 0, "scale_nbytes": 4}
            manifest = {"tensors": {"blocks.0.mlp.fc.weight": descriptor}, "activation_scales": {"transformer.h.0.mlp.c_fc.input": [0.001, 0.001]}}
            (package / "manifest.json").write_text(json.dumps(manifest))
            (package / "weights.bin").write_bytes(bytes([127, 128]))
            (package / "scales.bin").write_bytes(struct.pack("<f", 0.001))
            first = gate.validate_narrowing_bounds(package)
            self.assertTrue(first["signed_53_bit_product_valid"])
            manifest["activation_scales"]["transformer.h.0.mlp.c_fc.input"] = [0.99, 0.99]
            (package / "manifest.json").write_text(json.dumps(manifest))
            (package / "scales.bin").write_bytes(struct.pack("<f", 0.99))
            second = gate.validate_narrowing_bounds(package)
            self.assertNotEqual(first["package_digest"], second["package_digest"])
            self.assertFalse(second["signed_53_bit_product_valid"])
            self.assertGreater(second["maximum_absolute_product_bound"], first["maximum_absolute_product_bound"])

    def test_family_requantization_can_use_full_width_with_other_narrowing(self):
        from scripts.pipeline.lower_fixed_point_mlp_crossing_to_calyx import _requantize_phase
        cells, wires, control = _requantize_phase(
            "probe", "acc", "scales", "bias", "out_scale", "post", "codes", "dequant",
            4, 4, narrow_products=True, requantization_width=64)
        self.assertIn("probe_real_product = std_smult_pipe(64);", cells)
        self.assertTrue(control)

if __name__ == "__main__":
    unittest.main()
