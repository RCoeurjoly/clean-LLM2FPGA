"""Exercise the compilation-only source tree supplied by the Nix boundary."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class ProductionSourceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.source = Path(os.environ.get('LLM2FPGA_COMPILE_SOURCE',
                                         Path(__file__).resolve().parents[1]))

    def test_verification_and_legacy_lowering_are_unavailable(self):
        for name in ('artifacts/reference', 'scripts/comparison', 'tests',
                     'TinyStories/model_adapter_exact_package.py',
                     'TinyStories/family_fixed_point_oracle.py',
                     'scripts/pipeline/lower_fixed_point_model_to_calyx.py',
                     'scripts/pipeline/fix_sv_synthesis_frontend.py',
                     'scripts/pipeline/run_bram_production_sv.py'):
            with self.subTest(name=name):
                self.assertFalse((self.source / name).exists(),
                                 'compilation can access excluded source: ' + name)

    def test_real_core_and_frontend_import_from_filtered_source(self):
        code = '''import sys
from pathlib import Path
from tempfile import TemporaryDirectory
sys.path.insert(0,sys.argv[1])
from compiler.build_core import build_core
from compiler.build_profile import CoreBuildProfile
from compiler.pipeline import CompilerTool
from compiler.frontend.package_model import build_fixed_point_model
from compiler.generation import capture_generation_contexts
from compiler.ddr_bank_adapter import lower_ddr_bank_adapter, emit_ddr_bank_adapter_stage
from compiler.ddr_transport import build_ddr_transport, verify_ddr_transport_receipt
from compiler.clock_contract import qualify_external_clock_contract
assert callable(build_ddr_transport) and callable(verify_ddr_transport_receipt) and callable(qualify_external_clock_contract)
assert callable(lower_ddr_bank_adapter) and callable(emit_ddr_bank_adapter_stage)
assert callable(build_core) and callable(capture_generation_contexts)
with TemporaryDirectory() as temporary:
    root=Path(temporary)
    tools={}
    for name in ('torch','circt','memory','translate','calyx'):
        path=root/name
        path.write_text('tool identity')
        tools[name]=CompilerTool(path)
    library=root/'library'
    library.mkdir()
    (library/'primitive.futil').write_text('primitive identity')
    contract=Path(sys.argv[1])/'compiler/contracts/tinystories-1m-fixed-point.json'
    CoreBuildProfile(contract,tools,library).validate()
'''
        result = subprocess.run([sys.executable, '-I', '-c', code, str(self.source)],
                                cwd='/tmp', capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
