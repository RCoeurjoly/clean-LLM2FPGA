"""Loaded numerical replay cannot consume an unrelated simulator receipt."""
import importlib.util
from pathlib import Path
import shutil
import tempfile
import unittest
from compiler.stages import StageSpec,digest,run_stage


class LoadedSimulatorAuthenticationTests(unittest.TestCase):
    def test_declared_hashes_do_not_substitute_for_compiling_all_components(self):
        name='scripts.comparison.graph_loaded_simulator'
        module=__import__(name,fromlist=['validate_native_inputs'])
        validator=getattr(module,'validate_native_inputs',None)
        self.assertIsNotNone(validator,'native command coverage validation missing')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            library=root/'library.sv';core=root/'component-0000.sv'
            library.write_text('module library; endmodule')
            core.write_text('module main; endmodule')
            required={str(p):digest(p) for p in (library,core)}
            spec={'input_hashes':required,'argv':['verilator',str(library)]}
            with self.assertRaisesRegex(ValueError,'all core RTL'):
                validator(spec,required,(library,core))

    def test_onchip_receipt_cannot_be_labeled_loaded(self):
        name='scripts.comparison.graph_loaded_simulator'
        self.assertIsNotNone(importlib.util.find_spec(name),'loaded simulator authentication missing')
        module=__import__(name,fromlist=['authenticate_loaded_simulator'])
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'input';source.write_text('synthetic wrong-stage artifact')
            tool=Path(shutil.which('cp')).resolve();output=root/'build'
            run_stage(StageSpec('graph-generation-simulator',
                (str(tool),str(source),'simulator'),{str(source):digest(source)},
                {str(tool):digest(tool)},{},('simulator',)),output)
            with self.assertRaisesRegex(ValueError,'loaded simulator stage'):
                module.authenticate_loaded_simulator(output,root/'core',None,root/'transport')

    def test_incomplete_loaded_build_is_rejected(self):
        name='scripts.comparison.graph_loaded_simulator'
        self.assertIsNotNone(importlib.util.find_spec(name),'loaded simulator authentication missing')
        module=__import__(name,fromlist=['authenticate_loaded_simulator'])
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaisesRegex(ValueError,'stage'):
                module.authenticate_loaded_simulator(root/'build',root/'core',None,root/'transport')
