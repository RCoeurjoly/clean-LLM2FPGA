"""Production transport rejects stale/incompatible compilation before native work."""
import importlib, tempfile, unittest
from pathlib import Path
import test_graph_build_core as core_tests

class DDRTransportBoundaryTests(unittest.TestCase):
    setUp = core_tests.BuildCoreBoundaryTests.setUp
    def test_bram_profile_invalidates_transport_acceptance(self):
        from compiler.ddr_transport import build_ddr_transport
        transport=self.output/'transport';transport.mkdir(parents=True)
        (transport/'transport.json').write_text('{"old":true}')
        with self.assertRaisesRegex(ValueError,'off-chip'):
            build_ddr_transport(self.output,self.profile,self.root/'controller',transport,yosys=self.root/'yosys')
        self.assertFalse((transport/'transport.json').exists())
    def test_output_cannot_overwrite_core_inputs(self):
        from compiler.ddr_transport import build_ddr_transport
        self.output.mkdir()
        protected=self.output/'core.json';protected.write_text('preserve core')
        with self.assertRaisesRegex(ValueError,'overlap'):
            build_ddr_transport(self.output,self.profile,self.root/'controller',self.output,yosys=self.root/'yosys')
        self.assertEqual(protected.read_text(),'preserve core')
    def test_cli_requires_both_controller_and_yosys(self):
        from compiler.compile_core import parse_arguments
        args=[]
        for name in ('snapshot','package','numerical-contract','output','calyx-library','torch-tool','circt-tool','memory-tool','translate-tool','calyx-tool'):
            args+=['--'+name,'/tmp/input']
        with self.assertRaises(SystemExit):parse_arguments(args+['--controller-stage','/tmp/controller'])
        parsed=parse_arguments(args+['--controller-stage','/tmp/controller','--yosys-tool','/tmp/yosys','--offchip-immutable','--no-narrow-immutable'])
        self.assertEqual(parsed.controller_stage,Path('/tmp/controller'))

    def test_core_rebuild_invalidates_downstream_transport(self):
        from compiler.build_core import build_core
        transport=self.output/'transport'
        for name in ('transport.json','native-core/success.json','domains/qualification.json','domains/elaboration/success.json','adapter/emission/success.json'):
            path=transport/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('old receipt')
        with self.assertRaisesRegex(ValueError,'authenticated CompileInputs'):
            build_core(object(),self.profile,self.output)
        self.assertFalse(any((transport/name).exists() for name in ('transport.json','native-core/success.json','domains/qualification.json','domains/elaboration/success.json','adapter/emission/success.json')))
