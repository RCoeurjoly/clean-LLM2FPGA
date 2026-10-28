"""Controller generation consumes board inputs and native typed settings."""
import contextlib,io,json,shutil,subprocess,tempfile,unittest
from pathlib import Path

class LiteDRAMExportTests(unittest.TestCase):
    def pins(self):
        result={}; index=0
        for key,count in {'a':15,'ba':3,'dq':72,'dqs_p':9,'dqs_n':9,'clk_p':1,'clk_n':1,'cke':1,'cs_n':1,'odt':1}.items():
            result[key]=[f'P{n}' for n in range(index,index+count)];index+=count
        for key in ['ras_n','cas_n','we_n','reset_n']:result[key]=f'P{index}';index+=1
        return result
    def test_invalid_pin_geometry_and_duplicates_rejected(self):
        from compiler.litedram_export import validate_pins
        pins=self.pins();self.assertEqual(validate_pins(pins),pins)
        pins['dq'].pop()
        with self.assertRaisesRegex(ValueError,'dq'):validate_pins(pins)
        pins=self.pins();pins['dq'][0]=pins['a'][0]
        with self.assertRaisesRegex(ValueError,'duplicate'):validate_pins(pins)
        pins=self.pins();pins['dm']=['P900']
        with self.assertRaisesRegex(ValueError,'fields'):validate_pins(pins)
    def test_native_outputs_have_full_port_and_typed_initialization(self):
        from compiler.litedram_export import emit_controller
        with tempfile.TemporaryDirectory() as temporary,contextlib.redirect_stdout(io.StringIO()):
            root=Path(temporary);emit_controller(self.pins(),root)
            text=(root/'controller.v').read_text()
            self.assertIn('[575:0] user_port_native_0_wdata_data',text)
            contract=json.loads((root/'controller.json').read_text())
            self.assertIn('ports',contract,'native controller port contract is missing')
            self.assertEqual(contract['schema_version'],1)
            self.assertEqual(contract['module'],'closure_litedram')
            ports={port['name']:port for port in contract['ports']}
            self.assertEqual(ports['user_port_native_0_cmd_addr']['width'],25)
            self.assertEqual(ports['user_port_native_0_rdata_data']['width'],576)
            self.assertEqual(ports['user_port_native_0_wdata_we']['width'],72)
            self.assertEqual(ports['user_clk']['direction'],'output')
            self.assertEqual(ports['ddram_dq']['direction'],'inout')
            self.assertEqual(ports['ddram_dq']['width'],72)
            self.assertEqual(ports['init_done']['direction'],'output')
            self.assertEqual(len(ports),len(contract['ports']))
            native=subprocess.run([shutil.which('yosys'),'-p',
                'read_verilog -lib -sv controller.v; write_json ports.json'],
                cwd=root,capture_output=True,text=True,timeout=30)
            self.assertEqual(native.returncode,0,native.stderr)
            elaborated=json.loads((root/'ports.json').read_text())['modules']['closure_litedram']['ports']
            self.assertEqual(set(elaborated),set(ports))
            for name,port in elaborated.items():
                self.assertEqual(ports[name]['width'],len(port['bits']),name)
                self.assertEqual(ports[name]['direction'],port['direction'],name)
            self.assertNotIn('ddram_dm',text);self.assertNotIn('ODELAYE2',text)
            self.assertIn('wb_ctrl_adr',text);self.assertIn('init_done',text)
            init=json.loads((root/'initialization.json').read_text())
            self.assertTrue(any(row[2]==2 and row[1]==512 for row in init['sequence']))
            self.assertTrue(any(row[2]==0 and row[1]==2352 for row in init['sequence']))
            csr=json.loads((root/'csr.json').read_text())
            self.assertIn('ddrctrl_init_done',csr['csr_registers'])
            geometry=json.loads((root/'controller.json').read_text())
            self.assertEqual(geometry['data_width'],576);self.assertEqual(geometry['address_width'],25)
            self.assertFalse(geometry['calibration_verified'])

    def test_stage_authenticates_board_and_native_source_dependencies(self):
        import sys,litedram,litex,migen
        from compiler.litedram_export import export_controller_stage
        from compiler.stages import verify_stage_receipt
        with tempfile.TemporaryDirectory() as temporary,contextlib.redirect_stdout(io.StringIO()):
            root=Path(temporary);pins=root/'pins.json';pins.write_text(json.dumps(self.pins()))
            sources={name:Path(module.__file__).parent for name,module in [('litedram',litedram),('litex',litex),('migen',migen)]}
            receipt=export_controller_stage(pins,root/'stage',python=Path(sys.executable),sources=sources)
            dependencies=verify_stage_receipt(root/'stage')
            self.assertIn(str(pins.resolve()),dependencies)
            self.assertIn(str((sources['litedram']/'gen.py').resolve()),dependencies)
            self.assertIn(str((sources['litex']/'soc/integration/export.py').resolve()),dependencies)
            self.assertTrue((root/'stage/controller.v').exists())
            fake=root/'fake-litedram';fake.mkdir();(fake/'__init__.py').write_text('')
            wrong=dict(sources,litedram=fake)
            with self.assertRaises(Exception):export_controller_stage(pins,root/'wrong',python=Path(sys.executable),sources=wrong)
            self.assertFalse((root/'wrong/success.json').exists())
            pins.write_text('{}')
            with self.assertRaises(Exception):export_controller_stage(pins,root/'stage',python=Path(sys.executable),sources=sources)
            self.assertFalse((root/'stage/success.json').exists())

    def test_controller_retains_authenticated_raw_board_dependencies(self):
        import sys,litedram,litex,migen
        from compiler.litedram_export import export_controller_stage
        from compiler.stages import digest,verify_stage_receipt
        with tempfile.TemporaryDirectory() as temporary,contextlib.redirect_stdout(io.StringIO()):
            root=Path(temporary);pins=root/'pins.json';pins.write_text(json.dumps(self.pins()))
            raw=root/'memory.ucf';raw.write_text('authenticated raw board input')
            dependencies={str(raw.resolve()):digest(raw)}
            sources={name:Path(module.__file__).parent for name,module in [('litedram',litedram),('litex',litex),('migen',migen)]}
            export_controller_stage(pins,root/'stage',python=Path(sys.executable),sources=sources,
                                    input_dependencies=dependencies)
            self.assertIn(str(raw.resolve()),verify_stage_receipt(root/'stage'))
            raw.write_text('modified board input')
            with self.assertRaises(Exception):
                export_controller_stage(pins,root/'stage',python=Path(sys.executable),sources=sources,
                                        input_dependencies=dependencies)
            self.assertFalse((root/'stage/success.json').exists())
