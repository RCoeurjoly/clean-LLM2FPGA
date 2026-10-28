import os
from pathlib import Path
import subprocess,tempfile,unittest

ROOT=Path(__file__).resolve().parents[1]
YOSYS=Path(os.environ.get('LLM2FPGA_YOSYS','/nix/store/zgpbcqdyhw6d355xalrw4ishc3g5qhr5-llm2fpga-yosys-sdp72-parity/bin/yosys'))
MAP=ROOT/'rtl/techmap/closure_shiftx.v'

class StructuralShiftTests(unittest.TestCase):
    def prove(self,width,out,selector,low_zero=0,signed=False):
        self.assertTrue(MAP.is_file(),'native structural shift mapper missing')
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'fixture.v'
            sign='signed ' if signed else ''
            select='b' if low_zero==0 else '{b, '+str(low_zero)+"'b0}"
            source.write_text('module fixture(input ['+str(width-1)+':0] a, input '+sign+'['+str(selector-1)+':0] b, output ['+str(out-1)+':0] y); '+chr(92)+'$shiftx #(.A_SIGNED(0), .B_SIGNED('+str(int(signed))+'), .A_WIDTH('+str(width)+'), .B_WIDTH('+str(selector+low_zero)+'), .Y_WIDTH('+str(out)+')) shift_cell (.A(a), .B('+select+'), .Y(y)); endmodule')
            stages=['read_verilog -icells '+str(source),'prep -top fixture','design -stash gold','design -load gold','techmap -map '+str(MAP),'opt_expr -mux_undef -mux_bool -fine','opt_clean -purge','opt -fast']
            if not signed and (1<<low_zero)>=out:stages+=['select -assert-none t:$shiftx']
            else:stages+=['select -assert-count 1 t:$shiftx']
            stages+=['design -stash gate','design -copy-from gold -as gold fixture','design -copy-from gate -as gate fixture','equiv_make gold gate equiv','hierarchy -top equiv','equiv_simple -undef','equiv_status -assert']
            result=subprocess.run([str(YOSYS),'-p','; '.join(stages)],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout[-2000:]+result.stderr)
    def test_unsigned_shift_and_out_of_bounds(self):self.prove(17,5,6)
    def test_small_nonpower_width(self):self.prove(5,2,3)
    def test_full_width_result(self):self.prove(16,16,5)
    def test_known_low_selector_bits(self):self.prove(37,3,4,low_zero=2)
    def test_signed_selector_falls_back(self):self.prove(17,5,6,signed=True)
    def test_single_bit_and_wide_selector(self):self.prove(1,1,33)
    def test_selector_high_bits_overflow(self):self.prove(9,4,32)
    def test_actual_alignment_and_partial_word(self):self.prove(255,14,5,low_zero=4)
    def test_nonpower_depth_and_partial_last_word(self):self.prove(17,3,3,low_zero=2)
