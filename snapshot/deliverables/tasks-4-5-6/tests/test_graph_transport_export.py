"""Standalone transport export preserves input provenance and failure boundaries."""
import tempfile
import unittest
from pathlib import Path


class TransportExportTests(unittest.TestCase):
    def test_invalid_core_removes_outer_and_inner_acceptance(self):
        from scripts.pipeline.export_graph_ddr_transport import export_transport_stage
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'output'
            (output / 'transport').mkdir(parents=True)
            for name in ('success.json', 'transport/transport.json'):
                (output / name).write_text('old acceptance')
            with self.assertRaises(FileNotFoundError):
                export_transport_stage(root / 'core', root / 'controller', output,
                    numerical_contract=root / 'contract', calyx_library=root / 'library',
                    python=root / 'python', yosys=root / 'yosys')
            self.assertFalse((output / 'success.json').exists())
            self.assertFalse((output / 'transport/transport.json').exists())

    def test_output_overlap_preserves_core(self):
        from scripts.pipeline.export_graph_ddr_transport import export_transport_stage
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / 'success.json'
            marker.write_text('preserve input')
            with self.assertRaisesRegex(ValueError, 'overlap'):
                export_transport_stage(root / 'core', root / 'controller', root,
                    numerical_contract=root / 'contract', calyx_library=root / 'library',
                    python=root / 'python', yosys=root / 'yosys')
            self.assertEqual(marker.read_text(), 'preserve input')

    def test_native_child_overlap_also_preserves_input(self):
        from scripts.pipeline.export_graph_ddr_transport import main
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / 'transport.json'
            marker.write_text('preserve input')
            args = ['--native-join']
            for name, path in [('core',root/'core'), ('controller',root/'controller'),
                               ('output',root), ('numerical-contract',root/'contract'),
                               ('calyx-library',root/'library'), ('yosys',root/'yosys')]:
                args += ['--'+name, str(path)]
            with self.assertRaisesRegex(ValueError, 'overlap'):
                main(args)
            self.assertEqual(marker.read_text(), 'preserve input')
