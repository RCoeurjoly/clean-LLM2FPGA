"""Shared-clock bank qualification; does not claim CDC or board acceptance."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

TB = r'''module tb;
 reg clk=0;always #5 clk=~clk;
 reg reset=1,ready=0;reg [2:0] en=0,we=0;
 reg [95:0] addr=0;wire [191:0] data;wire [2:0] done;
 wire fault,cv,cw,rr;wire[24:0]ca;
 reg cr=0,rv=0;reg [575:0]rd=0;
 integer state=0,count=0,requests=0,delay_cycles=1,cycles=0,mode=0;
 integer order[0:6];reg[24:0]accepted=0;
 closure_ddr_read_bank #(.CLIENTS(3),.FRAME_COUNT(6),
 .DEPTHS({32'd9,32'd10,32'd19}),.BASE_FRAMES({32'd5,32'd3,32'd0})) dut(
 .clk(clk),.reset(reset),.memory_ready(ready),.content_en(en),.write_en(we),
 .addr(addr),.read_data(data),.done(done),.fault(fault),.cmd_valid(cv),
 .cmd_ready(cr),.cmd_we(cw),.cmd_addr(ca),.rdata_valid(rv),.rdata_ready(rr),.rdata(rd));
 initial begin
  if(!$value$plusargs("DELAY=%d",delay_cycles))delay_cycles=1;
  if(!$value$plusargs("MODE=%d",mode))mode=0;
  repeat(3)@(negedge clk);reset=0;
  addr={32'd8,32'd9,32'd18};en=3'b111;
  repeat(5)begin @(negedge clk);if(cv||done)$fatal(1,"issued before ready");end
  ready=1;
  if(mode!=0)begin
   case(mode)
    1:we[1]=1;
    2:addr[63:32]=10;
    3:begin wait(cv);@(negedge clk);addr[31:0]=0;end
    4:begin wait(cv);@(negedge clk);en[0]=0;end
    5:begin wait(done[0]);@(negedge clk);addr[31:0]=0;end
   endcase
   wait(fault);repeat(5)begin @(negedge clk);if(cv||rr||done)$fatal(1,"fault allows successful service");end
   $display("PASS rejected-client mode=%0d",mode);$finish;
  end
  wait(&done);@(negedge clk);
  if(fault||requests!=3||data!=={64'd53,64'd36,64'd18})$fatal(1,"bad first round");
  if(order[0]!=2||order[1]!=4||order[2]!=5)$fatal(1,"unfair first round");
  // Two completed clients remain asserted while client zero asks again.
  en[0]=0;wait(!done[0]);@(negedge clk);addr[31:0]=8;en[0]=1;
  wait(done[0]);@(negedge clk);
  if(fault||requests!=4||data!=={64'd53,64'd36,64'd8})$fatal(1,"held completion blocked another client");
  repeat(5)@(negedge clk);if(requests!=4)$fatal(1,"duplicate held command");
  en=0;wait(done==0);repeat(3)@(negedge clk);
  // New simultaneous requests start after the last granted client.
  addr={32'd0,32'd0,32'd9};en=3'b111;
  wait(&done);@(negedge clk);
  if(fault||requests!=7||data!=={64'd45,64'd27,64'd9})$fatal(1,"bad second round");
  if(order[4]!=3||order[5]!=5||order[6]!=1)$fatal(1,"round robin did not rotate");
  $display("PASS clients=3 requests=7 independent-retirement");$finish;
 end
 always @(posedge clk)begin
  cycles<=cycles+1;if(cycles>3000)$fatal(1,"timeout");
  if(reset)begin state<=0;cr<=0;rv<=0;end else case(state)
   0:if(cv)begin accepted<=ca;count<=3;state<=1;if(cw)$fatal(1,"write command");end
   1:begin
    if(!cv||ca!==accepted)begin
     if(mode==0)$fatal(1,"unstable command");else state<=0;
    end else if(count==0)begin cr<=1;state<=2;end else count<=count-1;
   end
   2:begin
    cr<=0;
    if(cv)begin order[requests]<=ca;requests<=requests+1;count<=delay_cycles;state<=3;end
    else state<=0;
   end
   3:if(count==0)begin
    rd<={64'(accepted*9+8),64'(accepted*9+7),64'(accepted*9+6),
         64'(accepted*9+5),64'(accepted*9+4),64'(accepted*9+3),
         64'(accepted*9+2),64'(accepted*9+1),64'(accepted*9)};
    rv<=1;state<=4;
   end else count<=count-1;
   4:if(rr)begin rv<=0;state<=0;end
  endcase
 end
endmodule
'''


class DDRReadBankTests(unittest.TestCase):
    def _run(self, modes, delays):
        primitives = Path(__file__).resolve().parents[1] / 'rtl/primitives'
        bank = primitives / 'closure_ddr_read_bank.sv'
        self.assertTrue(bank.exists(), 'owned shared DDR read bank missing')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'tb.sv').write_text(TB)
            result = subprocess.run([shutil.which('iverilog'), '-g2012', '-s', 'tb',
                '-o', str(root / 'simulator'), str(primitives / 'closure_ddr_read_port.sv'),
                str(bank), str(root / 'tb.sv')], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            for mode in modes:
                for delay in delays:
                    result = subprocess.run([shutil.which('vvp'), str(root / 'simulator'),
                        f'+MODE={mode}', f'+DELAY={delay}'], capture_output=True, text=True, timeout=30)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('PASS ', result.stdout)

    def test_fair_service_with_independent_retirement_and_variable_latency(self):
        self._run([0], [1, 7, 29])

    def test_invalid_request_does_not_complete_or_continue_service(self):
        self._run(range(1, 6), [7])

    def test_actual_257_client_count_all_clients_receive_distinct_images(self):
        clients = 257
        depths = ','.join("32'd19" for _ in range(clients))
        bases = ','.join(f"32'd{i*3}" for i in reversed(range(clients)))
        testbench = r'''module tb;
 reg clk=0;always #5 clk=~clk;
 reg reset=1;reg [256:0]en=0;
 reg [8223:0]addr=0;wire[16447:0]data;wire[256:0]done;
 wire fault,cv,cw,rr;wire[24:0]ca;
 reg cr=0,rv=0;reg[575:0]rd=0;
 integer state=0,count=0,requests=0,cycles=0,i,lane;
 reg[24:0]accepted=0;
 closure_ddr_read_bank #(.CLIENTS(257),.FRAME_COUNT(771),
 .DEPTHS({DEPTH_VALUES}),.BASE_FRAMES({BASE_VALUES})) dut(
 .clk(clk),.reset(reset),.memory_ready(1'b1),.content_en(en),.write_en(257'd0),
 .addr(addr),.read_data(data),.done(done),.fault(fault),.cmd_valid(cv),
 .cmd_ready(cr),.cmd_we(cw),.cmd_addr(ca),.rdata_valid(rv),.rdata_ready(rr),.rdata(rd));
 initial begin
  for(i=0;i<257;i=i+1)addr[i*32+:32]=i%19;
  repeat(3)@(negedge clk);reset=0;en={257{1'b1}};
  wait(&done);@(negedge clk);
  if(fault||requests!=257)$fatal(1,"missing client or bank fault");
  for(i=0;i<257;i=i+1)if(data[i*64+:64]!==64'(i*27+i%19))$fatal(1,"wrong client image %0d",i);
  repeat(10)@(negedge clk);if(requests!=257)$fatal(1,"duplicate held completion");
  en=0;wait(done==0);
  $display("PASS clients=257 requests=257 distinct-images");$finish;
 end
 always @(posedge clk)begin
  cycles<=cycles+1;if(cycles>30000)$fatal(1,"timeout");
  if(reset)begin state<=0;cr<=0;rv<=0;end else case(state)
   0:if(cv)begin accepted<=ca;count<=requests%5;state<=1;if(cw)$fatal(1,"write command");end
   1:begin
    if(!cv||ca!==accepted)$fatal(1,"unstable command");
    if(count==0)begin cr<=1;state<=2;end else count<=count-1;
   end
   2:begin
    if(!cv||ca!=requests*3+(requests%19)/9)$fatal(1,"wrong grant/image");
    $display("grant=%0d cycle=%0d",requests,cycles);cr<=0;requests<=requests+1;count<=1+requests%29;state<=3;
   end
   3:if(count==0)begin
    for(lane=0;lane<9;lane=lane+1)rd[lane*64+:64]<=64'(accepted*9+lane);
    rv<=1;state<=4;
   end else count<=count-1;
   4:if(rr)begin rv<=0;state<=0;end
  endcase
 end
endmodule
'''
        testbench = testbench.replace('DEPTH_VALUES', depths).replace('BASE_VALUES', bases)
        primitives = Path(__file__).resolve().parents[1] / 'rtl/primitives'
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'tb.sv').write_text(testbench)
            result = subprocess.run([shutil.which('iverilog'), '-g2012', '-s', 'tb',
                '-o', str(root / 'simulator'), str(primitives / 'closure_ddr_read_port.sv'),
                str(primitives / 'closure_ddr_read_bank.sv'), str(root / 'tb.sv')],
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([shutil.which('vvp'), str(root / 'simulator')],
                capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('PASS clients=257 requests=257 distinct-images', result.stdout)

    def test_overlapping_or_out_of_capacity_images_are_rejected(self):
        primitives = Path(__file__).resolve().parents[1] / 'rtl/primitives'
        for depths, bases, frames, diagnostic in (
                ("{32'd9,32'd10,32'd19}", "{32'd4,32'd3,32'd0}", 6, 'overlapping'),
                ("{32'd9,32'd10,32'd19}", "{32'd5,32'd3,32'd0}", 5, 'outside'),
                ("{32'd0,32'd10,32'd19}", "{32'd5,32'd3,32'd0}", 6, 'outside')):
            fixture = TB.replace("{32'd9,32'd10,32'd19}", depths).replace(
                "{32'd5,32'd3,32'd0}", bases).replace('.FRAME_COUNT(6)', f'.FRAME_COUNT({frames})')
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / 'tb.sv').write_text(fixture)
                result = subprocess.run([shutil.which('iverilog'), '-g2012', '-s', 'tb',
                    '-o', str(root / 'simulator'), str(primitives / 'closure_ddr_read_port.sv'),
                    str(primitives / 'closure_ddr_read_bank.sv'), str(root / 'tb.sv')],
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                result = subprocess.run([shutil.which('vvp'), str(root / 'simulator')],
                    capture_output=True, text=True, timeout=30)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(diagnostic, result.stdout + result.stderr)
