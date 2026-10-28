"""Digital protocol checks for bundled-data CDC, not analog timing signoff."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

TB = r'''module tb;
 integer core_half=17,ui_half=6,phase=1,delay_cycles=1,pulse=0;
 reg core_clk=0,ui_clk=0,reset=1,ready=0;
 initial begin if(!$value$plusargs("CORE=%d",core_half))core_half=17;forever #(core_half)core_clk=~core_clk;end
 initial begin
  if(!$value$plusargs("UI=%d",ui_half))ui_half=6;
  if(!$value$plusargs("PHASE=%d",phase))phase=1;
  #(phase);forever #(ui_half)ui_clk=~ui_clk;
 end
 reg cv=0,cw=0,rr=0;reg[24:0]ca=0;
 wire cr,rv,mr,fault;wire[575:0]rd;
 wire ncv,ncw,nrr;wire[24:0]nca;
 reg ncr=0,nrv=0;reg[575:0]nrd=0;
 integer state=0,count=0,requests=0,i,core_lane,ui_lane,core_cycles=0,mode=0;
 function [63:0] word(input integer frame, input integer lane);
  word=64'hfedcba9876543210 ^ (64'h9e3779b97f4a7c15 * (frame*9+lane));
 endfunction
 reg[24:0]accepted=0;reg[575:0]snapshot=0;
 closure_ddr_read_cdc dut(.core_clk(core_clk),.ui_clk(ui_clk),.reset(reset),
 .ui_memory_ready(ready),.core_memory_ready(mr),.fault(fault),
 .core_cmd_valid(cv),.core_cmd_ready(cr),.core_cmd_we(cw),.core_cmd_addr(ca),
 .core_rdata_valid(rv),.core_rdata_ready(rr),.core_rdata(rd),
 .ui_cmd_valid(ncv),.ui_cmd_ready(ncr),.ui_cmd_we(ncw),.ui_cmd_addr(nca),
 .ui_rdata_valid(nrv),.ui_rdata_ready(nrr),.ui_rdata(nrd));
 initial begin
  if(!$value$plusargs("DELAY=%d",delay_cycles))delay_cycles=1;
  if(!$value$plusargs("MODE=%d",mode))mode=0;
  if(!$value$plusargs("PULSE=%d",pulse))pulse=0;
  repeat(7)@(negedge core_clk);reset=0;cv=1;ca=0;
  repeat(6)begin @(negedge core_clk);if(cr||mr||ncv)$fatal(1,"accepted before ready");end
  ready=1;
  if(mode==1)begin
   cw=1;wait(fault);repeat(6)@(negedge ui_clk);
   if(cr||rv||ncv||nrr||requests)$fatal(1,"invalid write issued read");
   $display("PASS invalid-write");$finish;
  end
  for(i=0;i<19;i=i+1)begin
   ca=i;cv=1;rr=0;
   do @(posedge core_clk);while(!cr);
   @(negedge core_clk);cv=0;ca=25'h1ffffff;
   // Change the request payload after acceptance: UI must use its snapshot.
   wait(rv);@(negedge core_clk);snapshot=rd;
   for(core_lane=0;core_lane<9;core_lane=core_lane+1)
    if(rd[core_lane*64+:64]!==word(i,core_lane))$fatal(1,"torn/wrong frame %0d lane%0d",i,core_lane);
   repeat(5)begin @(negedge core_clk);if(!rv||rd!==snapshot||cr)$fatal(1,"response changed before retirement");end
   rr=1;@(posedge core_clk);@(negedge core_clk);rr=0;
   wait(cr);
  end
  if(fault||requests!=19)$fatal(1,"missing/duplicate transactions");
  // Coordinated reset also resets the native peer; replay begins cleanly.
  reset=1;ready=0;cv=0;repeat(7)@(negedge core_clk);reset=0;
  ready=1;wait(cr);@(negedge core_clk);ca=23;cv=1;
  do @(posedge core_clk);while(!cr);
  @(negedge core_clk);cv=0;rr=1;wait(rv);@(negedge core_clk);
  if(rd[63:0]!==word(23,0)||fault)$fatal(1,"reset replay wrong frame");
  $display("PASS cdc frames=19 reset-replay");$finish;
 end
 always @(posedge core_clk)begin core_cycles<=core_cycles+1;if(core_cycles>5000)$fatal(1,"timeout");end
 always @(posedge ui_clk)begin
  if(reset)begin state<=0;ncr<=0;nrv<=0;requests<=0;end else case(state)
   0:if(ncv)begin accepted<=nca;count<=3;state<=1;if(ncw)$fatal(1,"native write");end
   1:begin
    if(!ncv||nca!==accepted)$fatal(1,"unstable native command");
    if(count==0)begin ncr<=1;state<=2;end else count<=count-1;
   end
   2:begin
    if(!ncv||nca!==accepted)$fatal(1,"command not held until acceptance");
    ncr<=0;requests<=requests+1;count<=delay_cycles;state<=3;
   end
   3:if(count==0)begin
    for(ui_lane=0;ui_lane<9;ui_lane=ui_lane+1)nrd[ui_lane*64+:64]<=word(accepted,ui_lane);
    nrv<=1;state<=4;
   end else count<=count-1;
   4:begin
    if(pulse!=0&&!nrr)$fatal(1,"native response pulse missed");
    if(nrr||pulse!=0)begin nrv<=0;state<=0;end
   end
  endcase
 end
endmodule
'''


class DDRReadCDCTests(unittest.TestCase):
    def _run(self, mode, clocks, *, pulse=False):
        primitive = Path(__file__).resolve().parents[1] / 'rtl/primitives/closure_ddr_read_cdc.sv'
        self.assertTrue(primitive.exists(), 'owned DDR read CDC bridge missing')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'tb.sv').write_text(TB)
            result = subprocess.run([shutil.which('iverilog'), '-g2012', '-s', 'tb',
                '-o', str(root / 'simulator'), str(primitive), str(root / 'tb.sv')],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            for core, ui, phase, delay in clocks:
                result = subprocess.run([shutil.which('vvp'), str(root / 'simulator'),
                    f'+MODE={mode}', f'+CORE={core}', f'+UI={ui}',
                    f'+PHASE={phase}', f'+DELAY={delay}', f'+PULSE={int(pulse)}'], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('PASS ', result.stdout)

    def test_payload_snapshot_backpressure_and_retirement_across_clock_ratios(self):
        self._run(0, [(17,6,1,1), (17,6,5,29), (6,17,3,7), (7,11,9,29)])

    def test_write_request_is_rejected_before_native_command(self):
        self._run(1, [(17,6,1,1), (6,17,3,7)])

    def test_unbuffered_native_response_pulse_is_always_captured(self):
        self._run(0, [(17,6,1,0), (17,6,5,1), (6,17,3,0), (7,11,9,7)], pulse=True)
