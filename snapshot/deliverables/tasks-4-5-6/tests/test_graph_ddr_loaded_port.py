"""Image loading -> readback -> clock-crossed inference on one native port."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

TB=r'''module tb;
 reg cc=0,uc=0;integer ch=7,uh=11;
 always begin #(ch) cc=~cc;end
 always begin #(uh) uc=~uc;end
 reg reset=1,ready=0,iv=0;reg[575:0]idata=0;
 wire ir,loaded,lf,cf,mready;
 reg ccv=1,ccw=0,crr=0;reg[24:0]cca=0;
 wire ccr,crv;wire[575:0]crd;
 wire cv,cw,wv,rr;wire[24:0]ca,index;
 wire[575:0]wd;wire[71:0]we;
 reg nr=0,nwr=0,rv=0;reg[575:0]rd=0;
 reg[575:0]frames[0:2],memory[0:2],held_data=0;
 reg cp=0,dp=0,rp=0;reg[24:0]wa=0,ra=0;
 integer mode=0,order=0,delay=1,count=0,writes=0,reads=0,inference=0,i,j,cycles=0,accepts=0;
 closure_ddr_loaded_port #(.FRAME_COUNT(3)) dut(
 .core_clk(cc),.ui_clk(uc),.reset(reset),.controller_ready(ready),
 .input_valid(iv),.input_ready(ir),.input_data(idata),.frame_index(index),
 .ui_loaded(loaded),.ui_load_fault(lf),.core_fault(cf),.core_memory_ready(mready),
 .core_cmd_valid(ccv),.core_cmd_ready(ccr),.core_cmd_we(ccw),.core_cmd_addr(cca),
 .core_rdata_valid(crv),.core_rdata_ready(crr),.core_rdata(crd),
 .cmd_valid(cv),.cmd_ready(nr),.cmd_we(cw),.cmd_addr(ca),
 .wdata_valid(wv),.wdata_ready(nwr),.wdata(wd),.wdata_we(we),
 .rdata_valid(rv),.rdata_ready(rr),.rdata(rd));
 initial begin
  if(!$value$plusargs("CORE=%d",ch))ch=7;
  if(!$value$plusargs("UI=%d",uh))uh=11;
  if(!$value$plusargs("ORDER=%d",order))order=0;
  if(!$value$plusargs("DELAY=%d",delay))delay=1;
  if(!$value$plusargs("MODE=%d",mode))mode=0;
  $readmemh("frames.hex",frames);
  repeat(5)@(negedge uc);reset=0;
  repeat(8)begin @(negedge uc);if(ir||cv||wv||loaded||mready||ccr)$fatal(1,"before init");end
  ready=1;
  for(i=0;i<3;i=i+1)begin
   iv=1;idata=frames[i];do @(posedge uc);while(!ir);
   @(negedge uc);iv=0;idata=~frames[i];wait(index!=i||loaded||lf);
   @(negedge uc);
   if(mode!=0)begin
    if(!lf||loaded)$fatal(1,"bad readback accepted");
    repeat(30)begin @(negedge uc);if(cv||wv||ir||mready||ccr||crv)$fatal(1,"fault handoff");end
    if(inference!=0)$fatal(1,"inference after bad load");
    $display("PASS corrupt-load-blocked");$finish;
   end
  end
  if(!loaded||writes!=3||reads<3)$fatal(1,"incomplete load");
  // The first core request has remained pending throughout loading.
  for(j=0;j<3;j=j+1)begin
   wait(accepts==j+1);@(negedge cc);ccv=0;cca=25'h1ffffff;
   wait(crv);repeat(9)begin
    @(negedge cc);if(!crv||crd!==frames[j])$fatal(1,"response changed under backpressure");
   end
   crr=1;@(posedge cc);@(negedge cc);crr=0;
   if(j<2)begin cca=j+1;ccv=1;end
  end
  wait(ccr);if(cf||lf||inference!=3||writes!=3||reads!=6)$fatal(1,"handoff counts");
  reset=1;ccv=0;repeat(6)@(negedge uc);
  if(loaded||mready||cv||wv)$fatal(1,"reset failed");
  $display("PASS loaded=3 inference=3");$finish;
 end
 always @(posedge cc)begin
  if(reset)accepts<=0;
  else begin
   if(!loaded&&(ccr||mready||crv))$fatal(1,"core accepted before readback");
   if(ccv&&ccr)accepts<=accepts+1;
  end
 end
 always @(negedge uc)begin
  nr=ready&&!reset&&!cp&&!rp&&(!cw||order==0||dp);
  nwr=ready&&!reset&&!dp&&(order==1||cp);
 end
 always @(posedge uc)begin
  cycles<=cycles+1;if(cycles>5000)$fatal(1,"timeout");
  if(reset)begin cp<=0;dp<=0;rp<=0;rv<=0;writes<=0;reads<=0;inference<=0;end
  else begin
   if(loaded&&(wv||(cv&&cw)))$fatal(1,"write after handoff");
   if(cv&&nr)begin
    if(ca>2)$fatal(1,"bad address");
    if(cw)begin cp<=1;wa<=ca;writes<=writes+1;end
    else begin rp<=1;ra<=ca;count<=delay;reads<=reads+1;if(loaded)inference<=inference+1;end
   end
   if(wv&&nwr)begin
    if(we!=={72{1'b1}})$fatal(1,"partial frame");dp<=1;held_data<=wd;
   end
   if(cp&&dp)begin memory[wa]<=held_data;cp<=0;dp<=0;end
   if(rp&&!cp&&!dp&&!rv)begin
    if(count==0)begin rd<=mode!=0 ? memory[ra]^(576'd1<<575):memory[ra];rv<=1;end
    else count<=count-1;
   end
   if(rv)begin if(!rr)$fatal(1,"native pulse missed");rv<=0;rp<=0;end
  end
 end
endmodule
'''


class DDRLoadedPortTests(unittest.TestCase):
    def _run(self, mode):
        primitives=Path(__file__).resolve().parents[1]/'rtl/primitives'
        owned=primitives/'closure_ddr_loaded_port.sv'
        self.assertTrue(owned.exists(),'loading/inference native-port ownership missing')
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);(root/'tb.sv').write_text(TB)
            frames=[sum(((i*9+lane+1)*0x102030405060708 & ((1<<64)-1))<<(64*lane)
                        for lane in range(9)) for i in range(3)]
            (root/'frames.hex').write_text(''.join(f'{f:0144x}\n' for f in frames))
            result=subprocess.run([shutil.which('iverilog'),'-g2012','-s','tb','-o',str(root/'sim'),
                str(owned),str(primitives/'closure_ddr_image_loader.sv'),
                str(primitives/'closure_ddr_read_cdc.sv'),str(root/'tb.sv')],
                capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            for core,ui in [(7,11),(17,6),(6,17)]:
                for order in [0,1]:
                    for delay in [1,7,29]:
                        result=subprocess.run([shutil.which('vvp'),str(root/'sim'),
                            f'+CORE={core}',f'+UI={ui}',f'+ORDER={order}',f'+DELAY={delay}',f'+MODE={mode}'],
                            cwd=root,capture_output=True,text=True,timeout=30)
                        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                        self.assertIn('PASS ',result.stdout)

    def test_loading_owns_port_until_verified_then_reads_cross_clocks(self):
        self._run(0)

    def test_corrupted_image_never_enables_inference(self):
        self._run(1)
