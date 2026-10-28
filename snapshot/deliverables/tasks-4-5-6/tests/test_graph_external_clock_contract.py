"""Use native elaborated connectivity, not RTL text, for shared memory domains."""
import importlib,json,shutil,tempfile,unittest
from pathlib import Path

class ClockContractTests(unittest.TestCase):
    def qualify(self, root, assignment='clk'):
        name='scripts.comparison.verify_graph_external_clock_contract'
        self.assertIsNotNone(importlib.util.find_spec(name),'native clock-contract verifier is missing')
        module=importlib.import_module(name)
        source=root/'core.sv'
        source.write_text('module main(input clk, reset, gate, output m_clk, m_reset);\n'
                          f'assign m_clk = {assignment}; assign m_reset = reset; endmodule\n')
        interface={'schema_version':1,'entrypoint':'main','ports':[
            {'name':n,'width':1,'direction':'input'} for n in ('clk','reset')],
            'memories':[{'cell':'m','ports':[{'name':'m_'+role,'role':role,'width':1,'direction':'output'} for role in ('clk','reset')]}]}
        path=root/'interface.json';path.write_text(json.dumps(interface))
        return module.qualify_external_clock_contract(source,path,root/'check',Path(shutil.which('yosys')))
    def test_direct_aliases_are_qualified(self):
        with tempfile.TemporaryDirectory() as directory:
            report=self.qualify(Path(directory))
            self.assertEqual(report['checked_memory_ports'],2)
            self.assertEqual(report['method'],'native Yosys net identity')
    def test_inverted_clock_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            with self.assertRaisesRegex(ValueError,'does not alias core clk'):self.qualify(root,'~clk')
            self.assertFalse((root/'check/qualification.json').exists())
    def test_gated_clock_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'does not alias core clk'):self.qualify(Path(directory),'clk & gate')
    def test_constant_clock_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError,'does not alias core clk'):self.qualify(Path(directory),"1'b0")
