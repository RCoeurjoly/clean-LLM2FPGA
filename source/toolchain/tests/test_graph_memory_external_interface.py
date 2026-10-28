"""Serialize external memory ports from bound typed IR, never from RTL text."""
import json,os,subprocess,tempfile,unittest
from pathlib import Path

SOURCE='''module attributes {calyx.entrypoint = "main"} {
 hw.module.extern @memory(in %addr0: i2, in %clk: i1, in %reset: i1,
   in %content_en: i1, in %write_en: i1, in %write_data: i64,
   out read_data: i64, out done: i1)
 calyx.component @main(%clk: i1 {clk}, %reset: i1 {reset}, %go: i1 {go}) -> (%done: i1 {done}) {
  %weights.addr, %weights.clk, %weights.reset, %weights.en, %weights.we,
  %weights.write, %weights.read, %weights.done = calyx.primitive @weights of @memory
   {external = true, llm2fpga.ref = false,
    llm2fpga.binding = {width = 64 : i64, depth = 3 : i64,
      address_width = 2 : i64, implementation = "ddr3",
      read_latency = 0 : i64, writable = false}} : i2, i1, i1, i1, i1, i64, i64, i1
  calyx.wires {calyx.assign %done = %go : i1}
  calyx.control {}
 } {toplevel}
}'''

class ExternalInterfaceTests(unittest.TestCase):
    def export(self,source):
        with tempfile.TemporaryDirectory() as temporary:
            path=Path(temporary)/'interface.json'
            result=subprocess.run([os.environ['LLM2FPGA_CALYX_PASS_TOOL'],'--verify-each',
              f'--llm2fpga-export-external-memory-interface=interface={path}'],
              input=source,text=True,capture_output=True,timeout=30)
            return result,json.loads(path.read_text()) if path.exists() else None
    def test_typed_binding_exports_port_names_widths_and_reversed_directions(self):
        result,interface=self.export(SOURCE);self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(interface['entrypoint'],'main')
        self.assertEqual(len(interface['memories']),1)
        memory=interface['memories'][0];self.assertEqual(memory['cell'],'weights')
        self.assertEqual(memory['depth'],3);self.assertEqual(memory['implementation'],'ddr3')
        ports={row['name']:row for row in memory['ports']}
        self.assertEqual(ports['weights_addr0']['width'],2)
        self.assertEqual(ports['weights_addr0']['direction'],'output')
        self.assertEqual(ports['weights_read_data']['width'],64)
        self.assertEqual(ports['weights_read_data']['direction'],'input')
        self.assertEqual(ports['weights_done']['direction'],'input')
        again,second=self.export(result.stdout);self.assertEqual(again.returncode,0,again.stderr)
        self.assertEqual(second,interface)
    def test_reference_only_memory_is_not_physical_interface(self):
        result,interface=self.export(SOURCE.replace('external = true','external = false').replace('llm2fpga.ref = false','llm2fpga.ref = true'))
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(interface['memories'],[])
    def test_binding_geometry_must_match_actual_typed_ports(self):
        result,interface=self.export(SOURCE.replace('address_width = 2 : i64','address_width = 3 : i64'))
        self.assertNotEqual(result.returncode,0);self.assertIsNone(interface)
        self.assertIn('bound external memory port contract',result.stderr)
