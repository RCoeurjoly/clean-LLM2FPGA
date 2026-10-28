"""Native image-loader protocol checks; physical controller remains a separate gate."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

TB=r'''module tb;
 reg clk=0;always #6 clk=~clk;
 reg reset=1,ready=0,iv=0;reg[575:0]idata=0;
 wire ir,loaded,fault,cv,cw,wv,rr;wire[24:0]ca,index;
 wire[575:0]wd;wire[71:0]we;
 reg cr=0,wr=0,rv=0;reg[575:0]rd=0;
 reg[575:0]memory[0:6],input_frames[0:6],held_write=0;
 reg command_pending=0,data_pending=0,read_pending=0;
 reg[24:0]write_address=0,read_address=0;
 reg cmd_stalled=0,data_stalled=0,last_we=0;reg[24:0]last_addr=0;
 reg[575:0]last_data=0;reg[71:0]last_mask=0;
 integer mode=0,order=0,cycles=0,writes=0,reads=0,verified=0,
   response_delay=1,write_delay=0,response_count=0,i,lane,started=0,report;
 closure_ddr_image_loader #(.FRAME_COUNT(7)) dut(
 .clk(clk),.reset(reset),.controller_ready(ready),.input_valid(iv),.input_ready(ir),
 .input_data(idata),.frame_index(index),.loaded(loaded),.fault(fault),
 .cmd_valid(cv),.cmd_ready(cr),.cmd_we(cw),.cmd_addr(ca),
 .wdata_valid(wv),.wdata_ready(wr),.wdata(wd),.wdata_we(we),
 .rdata_valid(rv),.rdata_ready(rr),.rdata(rd));
 initial begin
  $readmemh("frames.hex",input_frames);
  if(!$value$plusargs("ORDER=%d",order))order=0;
  if(!$value$plusargs("DELAY=%d",response_delay))response_delay=1;
  if(!$value$plusargs("MODE=%d",mode))mode=0;
  repeat(4)@(negedge clk);reset=0;iv=1;idata=input_frames[0];
  repeat(7)begin @(negedge clk);if(ir||cv||wv||loaded)$fatal(1,"load before controller ready");end
  ready=1;
  for(i=0;i<7;i=i+1)begin
   iv=1;idata=input_frames[i];
   do @(posedge clk);while(!ir);
   @(negedge clk);iv=0;idata=~input_frames[i];
   // Changing the upstream payload after acceptance cannot change native data.
   wait(index!=i||loaded||fault);@(negedge clk);
   if(mode!=0)begin
    if(!fault||loaded||index!=0)$fatal(1,"corruption did not stop image acceptance");
    repeat(5)begin @(negedge clk);if(cv||wv||rr||ir)$fatal(1,"fault continued native traffic");end
    $display("PASS readback-corruption");$finish;
   end
   if(fault)$fatal(1,"unexpected load fault");
  end
  if(!loaded||writes!=7||reads!=7||verified!=7)$fatal(1,"early or incomplete image acceptance");
  for(i=0;i<7;i=i+1)if(memory[i]!==input_frames[i])$fatal(1,"wrong stored frame");
  iv=1;repeat(6)begin @(negedge clk);if(ir||cv||wv||rr||!loaded)$fatal(1,"accepted extra image frame");end
  reset=1;iv=0;repeat(4)@(negedge clk);
  if(loaded||fault||index!=0)$fatal(1,"coordinated reset failed");
  $display("PASS frames=7 writes=7 verified=7");$finish;
 end
 always @(negedge clk)begin
  // Exercise both command-before-data and data-before-command acceptance.
  cr=ready&&!reset&&!command_pending&&!read_pending&&
      (!cw||order==0||data_pending);
  wr=ready&&!reset&&!data_pending&&(order==1||command_pending);
 end
 always @(posedge clk)begin
  cycles<=cycles+1;if(cycles>3000)$fatal(1,"timeout");
  if(reset)begin
   command_pending<=0;data_pending<=0;read_pending<=0;rv<=0;
   writes<=0;reads<=0;verified<=0;write_delay<=0;started<=0;
   cmd_stalled<=0;data_stalled<=0;
   for(lane=0;lane<7;lane=lane+1)memory[lane]<=0;
  end else begin
   if(cmd_stalled&&(!cv||ca!==last_addr||cw!==last_we))$fatal(1,"command changed under backpressure");
   if(data_stalled&&(!wv||wd!==last_data||we!==last_mask))$fatal(1,"write data changed under backpressure");
   cmd_stalled<=cv&&!cr;data_stalled<=wv&&!wr;
   if(cv&&!cr)begin last_addr<=ca;last_we<=cw;end
   if(wv&&!wr)begin last_data<=wd;last_mask<=we;end
   if(cv&&cr)begin
    if(ca>6)$fatal(1,"address outside image");
    if(cw)begin command_pending<=1;write_address<=ca;writes<=writes+1;end
    else begin read_pending<=1;read_address<=ca;reads<=reads+1;response_count<=response_delay;end
   end
   if(wv&&wr)begin
    if(we!=={72{1'b1}})$fatal(1,"partial image write mask");
    data_pending<=1;held_write<=wd;
   end
   if(command_pending&&data_pending)begin
    // Native command acceptance is not immediate physical write visibility.
    if(!started)begin started<=1;write_delay<=3;end
    else if(write_delay==0)begin
     memory[write_address]<=held_write;
     command_pending<=0;data_pending<=0;started<=0;
    end else write_delay<=write_delay-1;
   end
   if(read_pending&&!command_pending&&!data_pending&&!rv)begin
    if(response_count==0)begin
     rd<=mode!=0 ? memory[read_address] ^ (576'd1<<575) : memory[read_address];
     rv<=1;
    end else response_count<=response_count-1;
   end
   if(rv&&!rr)$fatal(1,"native response pulse missed");
   if(rv)begin rv<=0;read_pending<=0;verified<=verified+1;end
  end
 end
endmodule
'''


class DDRImageLoaderTests(unittest.TestCase):
    def _run(self, modes, orders, delays):
        primitive=Path(__file__).resolve().parents[1]/'rtl/primitives/closure_ddr_image_loader.sv'
        self.assertTrue(primitive.exists(),'owned DDR image loader missing')
        frames=[sum(((((1<<64)-1) ^ (i*9+lane)*0x9e3779b97f4a7c15) & ((1<<64)-1)) << (lane*64)
                    for lane in range(9)) for i in range(7)]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'tb.sv').write_text(TB)
            (root/'frames.hex').write_text(''.join(f'{frame:0144x}\n' for frame in frames))
            result=subprocess.run([shutil.which('iverilog'),'-g2012','-s','tb','-o',str(root/'simulator'),
                str(primitive),str(root/'tb.sv')],capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            for mode in modes:
                for order in orders:
                    for delay in delays:
                        result=subprocess.run([shutil.which('vvp'),str(root/'simulator'),f'+MODE={mode}',
                            f'+ORDER={order}',f'+DELAY={delay}'],cwd=root,capture_output=True,text=True,timeout=30)
                        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                        self.assertIn('PASS ',result.stdout)

    def test_every_frame_read_back_before_acceptance_with_independent_write_handshakes(self):
        self._run([0],[0,1],[1,7,29])

    def test_high_lane_readback_corruption_blocks_acceptance(self):
        self._run([1],[0,1],[7])
