"""Read-port boundary tests; clocks are shared, not a CDC qualification."""
from pathlib import Path
import shutil,subprocess,tempfile,unittest

TB='''module tb;
 reg clk=0; always #5 clk=~clk;
 reg reset=1,ready=0,en=0,we=0;reg [4:0] addr=0;
 wire [63:0] data;wire done,fault,cv,cw,rr;wire [24:0] ca;
 reg cr=0,rv=0;reg [575:0] rd=0,frames[0:2];reg [63:0] words[0:18];
 integer delay_cycles=1,requests=0,state=0,count=0,i,lane,cycles=0;
 reg [24:0] accepted=0;
 closure_ddr_read_port #(.DEPTH(19),.ADDRESS_WIDTH(5),.BASE_FRAME(5)) dut(
 .clk(clk),.reset(reset),.memory_ready(ready),.content_en(en),.write_en(we),
 .addr(addr),.read_data(data),.done(done),.fault(fault),.cmd_valid(cv),
 .cmd_ready(cr),.cmd_we(cw),.cmd_addr(ca),.rdata_valid(rv),.rdata_ready(rr),.rdata(rd));
 initial begin
  $readmemh("frames.hex",frames);$readmemh("words.hex",words);
  if(!$value$plusargs("DELAY=%d",delay_cycles))delay_cycles=1;
  repeat(3)@(negedge clk);reset=0;en=1;
  repeat(7)begin @(negedge clk);if(cv||done)$fatal(1,"issued before ready");end
  ready=1;
  for(i=0;i<19;i=i+1)begin
   addr=i;en=1;wait(done);@(negedge clk);
   if(fault||data!==words[i])$fatal(1,"bad word %0d",i);
   repeat(4)begin @(negedge clk);if(!done||data!==words[i])$fatal(1,"completion not held");end
   en=0;wait(!done);repeat(2)@(negedge clk);
  end
  if(requests!=19)$fatal(1,"duplicate or missing command");
  $display("PASS words=19 requests=%0d",requests);$finish;
 end
 always @(posedge clk)begin
  cycles<=cycles+1;if(cycles>10000)$fatal(1,"timeout");
  if(reset)begin state<=0;cr<=0;rv<=0;end else case(state)
   0:if(cv)begin accepted<=ca;count<=3;state<=1;if(cw)$fatal(1,"write command");end
   1:begin
    if(!cv||ca!==accepted)$fatal(1,"unstable command");
    if(count==0)begin cr<=1;state<=2;end else count<=count-1;
   end
   2:begin
    if(!cv||ca!==accepted)$fatal(1,"command vanished before acceptance");
    cr<=0;requests<=requests+1;count<=delay_cycles;state<=3;
   end
   3:if(count==0)begin rd<=frames[accepted-5];rv<=1;state<=4;end else count<=count-1;
   4:if(rr)begin rv<=0;state<=0;end
  endcase
 end
endmodule
'''

class DDRReadPortTests(unittest.TestCase):
    def test_all_words_with_backpressure_variable_response_and_retirement(self):
        primitive=Path(__file__).resolve().parents[1]/'rtl/primitives/closure_ddr_read_port.sv'
        self.assertTrue(primitive.exists(),'owned DDR read-port primitive missing')
        words=list(range(19));words[8]=(1<<64)-1;words[9]=0x123456789abcdef0
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'tb.sv').write_text(TB)
            (root/'words.hex').write_text(''.join(f'{v:016x}\n' for v in words))
            padded=words+[0]*8
            (root/'frames.hex').write_text(''.join(f'{sum(padded[n+j]<<(64*j) for j in range(9)):0144x}\n' for n in range(0,27,9)))
            result=subprocess.run([shutil.which('iverilog'),'-g2012','-s','tb','-o',str(root/'simulator'),str(primitive),str(root/'tb.sv')],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            for delay in (1,7,29):
                result=subprocess.run([shutil.which('vvp'),str(root/'simulator'),f'+DELAY={delay}'],cwd=root,capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn('PASS words=19 requests=19',result.stdout)

    def test_invalid_clients_fault_without_success_and_reset_restores_service(self):
        primitive=Path(__file__).resolve().parents[1]/'rtl/primitives/closure_ddr_read_port.sv'
        testbench='''module tb;
 reg clk=0;always #5 clk=~clk;
 reg reset=1,en=0,we=0,cr=0;reg [4:0]addr=0;
 wire [63:0]data;wire done,fault,cv,cw,rr;wire[24:0]ca;
 integer mode=0,cycles=0;
 closure_ddr_read_port #(.DEPTH(19),.ADDRESS_WIDTH(5),.BASE_FRAME(5)) dut(
 .clk(clk),.reset(reset),.memory_ready(1'b1),.content_en(en),.write_en(we),
 .addr(addr),.read_data(data),.done(done),.fault(fault),.cmd_valid(cv),
 .cmd_ready(cr),.cmd_we(cw),.cmd_addr(ca),.rdata_valid(1'b0),.rdata_ready(rr),.rdata(576'd0));
 initial begin
  if(!$value$plusargs("MODE=%d",mode))mode=0;
  repeat(3)@(negedge clk);reset=0;en=1;
  case(mode)
   0:we=1;
   1:addr=19;
   2:begin wait(cv);@(negedge clk);addr=1;end
   3:begin wait(cv);@(negedge clk);en=0;end
   4:begin wait(cv);@(negedge clk);cr=1;wait(rr);@(negedge clk);addr=1;end
  endcase
  wait(fault);repeat(4)begin @(negedge clk);if(done||cv||rr)$fatal(1,"fault reported successful transaction");end
  reset=1;en=0;we=0;cr=0;addr=9;
  repeat(3)@(negedge clk);reset=0;en=1;wait(cv);@(negedge clk);
  if(fault||done||cw||ca!=6)$fatal(1,"reset did not restore service");
  $display("PASS fault-and-reset");$finish;
 end
 always @(posedge clk)begin cycles<=cycles+1;if(cycles>200)$fatal(1,"timeout");end
endmodule
'''
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'tb.sv').write_text(testbench)
            result=subprocess.run([shutil.which('iverilog'),'-g2012','-s','tb','-o',str(root/'simulator'),str(primitive),str(root/'tb.sv')],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            for mode in range(5):
                result=subprocess.run([shutil.which('vvp'),str(root/'simulator'),f'+MODE={mode}'],capture_output=True,text=True,timeout=30)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                self.assertIn('PASS fault-and-reset',result.stdout)
