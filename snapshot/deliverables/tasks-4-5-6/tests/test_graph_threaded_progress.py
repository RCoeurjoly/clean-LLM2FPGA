"""Native threaded evaluation must preserve literal full-width public results."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from scripts.comparison.graph_rtl_runtime import emit_generation_testbench

EVIDENCE=Path(__file__).resolve().parents[3]/'artifacts/task6/full-model-numerical-2026-10-24'


@unittest.skipUnless(EVIDENCE.is_dir(),'requires retained goal study')
class ThreadedProgressTests(unittest.TestCase):
    def test_threaded_clocked_lanes_match_literal_results_and_single_thread(self):
        self.exercise_lanes(emit_generation_testbench,EVIDENCE/'parallel_progress_main.cpp')

    def exercise_lanes(self,emitter,driver,*,timing=True,thread_counts=(1,4)):
        self.assertTrue(driver.is_file(),'threaded observer missing')
        verilator=os.environ.get('LLM2FPGA_VERILATOR') or shutil.which('verilator')
        self.assertIsNotNone(verilator)
        clang=os.environ.get('LLM2FPGA_OBSERVER_CLANG')
        disable_slp=os.environ.get('LLM2FPGA_OBSERVER_DISABLE_SLP')=='1'
        if disable_slp:self.assertIsNotNone(clang,'no-SLP qualification requires explicit Clang')
        mask=2**64-1
        banks=[[0x0123456789abcdef^(lane<<32)^i^(3<<48) for i in range(64)] for lane in range(8)]
        expected=[[] for _ in range(16)]
        for phase in range(1072):
            if phase%67<65:expected[phase//67].append(banks[phase%8][(phase%67)%64])
            banks=[[(((v<<7)&mask)|(v>>57))^((v+0x9e3779b97f4a7c15+i)&mask)
                    for i,v in enumerate(bank)] for bank in banks]
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'tb.sv').write_text(emitter(65))
            (root/'active-groups.h').write_text('static void report_active_groups(const Vtb___024root&) {}\n')
            ports=['input clk,reset,go','output done']
            body=['integer phase=0;reg stall=0;initial if($value$plusargs("STALL=%d",stall))begin end',
                  'always @(posedge clk) if(reset)phase<=0;else if(go&&!stall&&phase<1073)phase<=phase+1;',
                  'assign done=phase==1073;wire[63:0]a,b,c,d,e,f,g,h;wire[5:0]sel=6\'(phase%67);']
            for lane,name in enumerate('abcdefgh'):
                body.append(f'lane #(.ID({lane})) unit_{name}(clk,reset,go&&!stall,sel,{name});')
            body.append('wire[63:0]value='+''.join(f'phase%8=={i}?{name}:' for i,name in enumerate('abcdefg'))+'h;')
            for role,bits in [('prompt',2),('generated_tokens',4),('last_logits',7)]:
                ports += [f'output[{bits-1}:0]{role}_addr0',f'output {role}_clk,{role}_reset,{role}_content_en,{role}_write_en',
                          f'output[63:0]{role}_write_data',f'input[63:0]{role}_read_data',f'input {role}_done']
                condition={'prompt':"1'b0",'generated_tokens':'phase<1072&&phase%67>=65','last_logits':'phase<1072&&phase%67<65'}[role]
                addr={'prompt':'0','generated_tokens':"4'(phase/67)",'last_logits':"7'(phase%67)"}[role]
                data={'prompt':"64'd0",'generated_tokens':"64'(phase/67)",'last_logits':'value'}[role]
                body += [f'assign {role}_clk=clk;assign {role}_reset=reset;',
                         f'assign {role}_content_en=go&&!stall&&({condition});assign {role}_write_en={role}_content_en;',
                         f'assign {role}_addr0={addr};assign {role}_write_data={data};']
            lane='''module lane #(parameter ID=0)(input clk,reset,go,input[5:0]sel,output[63:0]out);
reg[63:0]bank[0:63];integer i;integer reset_edges=0;
always @(posedge clk) begin
 if(reset)reset_edges<=reset_edges+1;
 for(i=0;i<64;i=i+1)
 if(reset)bank[i]<=64'h0123456789abcdef^(64'(ID)<<32)^64'(i)^(64'(reset_edges)<<48);
 else if(go)bank[i]<={bank[i][56:0],bank[i][63:57]}^(bank[i]+64'h9e3779b97f4a7c15+64'(i));
end
assign out=bank[sel];endmodule
'''
            (root/'main.sv').write_text('module main('+','.join(ports)+');\n'+'\n'.join(body)+'\nendmodule\n'+lane)
            outputs=[]
            for threads in thread_counts:
                build=root/f'build{threads}'
                cflags=f'-DVL_TIME_CONTEXT -DOBSERVER_CONTEXT_THREADS={threads} -I{root}'
                if disable_slp:cflags+=' -fno-slp-vectorize'
                argv=[verilator,'--cc','--exe','--assert','--stats','--threads',str(threads),
                    '--top-module','tb','-Wno-fatal','--Mdir',str(build),'-o','observer','-j','1',
                    '-CFLAGS',cflags,str(driver),str(root/'tb.sv'),str(root/'main.sv')]
                if timing:argv.insert(3,'--timing')
                if not clang:argv.insert(3,'--build')
                built=subprocess.run(argv,capture_output=True,text=True,timeout=180)
                self.assertEqual(built.returncode,0,built.stdout+built.stderr)
                self.assertNotIn('UNOPTTHREADS',built.stderr,'fixture must admit the requested parallel schedule')
                if clang:
                    compiled=subprocess.run(['make','-C',str(build),'-f','Vtb.mk','-j1',f'CXX={clang}',f'LINK={clang}',
                        'CFG_CXXFLAGS_STD=-std=c++20','CFG_CXXFLAGS_COROUTINES=',
                        'CFG_CXXFLAGS_NO_UNUSED=-Wno-unused-variable -Wno-unused-parameter',
                        'CFG_GCH_IF_CLANG=.gch','CFG_CXXFLAGS_PCH_I=-include-pch','OPT_FAST=-O2','OPT_SLOW=-O2'],
                        capture_output=True,text=True,timeout=180)
                    self.assertEqual(compiled.returncode,0,compiled.stdout+compiled.stderr)
                runtime=root/f'run{threads}';runtime.mkdir();(runtime/'prompt.hex').write_text('1\n2\n3\n4\n')
                run=subprocess.run([str(build/'observer'),'+OBSERVER_CAPTURE_STEPS=1'],cwd=runtime,capture_output=True,text=True,timeout=30)
                self.assertEqual(run.returncode,0,run.stdout+run.stderr)
                self.assertIn(f'host_threads={threads} model_threads={threads}',run.stderr)
                self.assertEqual([line for line in run.stdout.splitlines() if line.startswith('TOKEN ')],
                                 [f'TOKEN {i} {i:016x}' for i in range(16)])
                outputs.append([line for line in run.stdout.splitlines() if line.startswith(('TOKEN ','LOGIT ','CYCLES '))])
                self.assertIn('CYCLES 1073',outputs[-1])
                for step in range(16):
                    words=[int(s,16) for s in (runtime/f'step-{step:02d}.hex').read_text().splitlines()]
                    self.assertEqual(words,expected[step])
                if threads==thread_counts[-1]:
                    again=root/'repeat';again.mkdir();(again/'prompt.hex').write_text('1\n2\n3\n4\n')
                    repeated=subprocess.run([str(build/'observer'),'+OBSERVER_CAPTURE_STEPS=1'],cwd=again,capture_output=True,text=True,timeout=30)
                    self.assertEqual(repeated.returncode,0,repeated.stderr)
                    for step in range(16):
                        self.assertEqual((again/f'step-{step:02d}.hex').read_bytes(),(runtime/f'step-{step:02d}.hex').read_bytes())
                    partial=root/'partial';partial.mkdir();(partial/'prompt.hex').write_text('1\n2\n3\n4\n')
                    stop=subprocess.run([str(build/'observer'),'+OBSERVER_CAPTURE_STEPS=1','+OBSERVER_STOP_AFTER_STEPS=1'],cwd=partial,capture_output=True,text=True,timeout=30)
                    self.assertEqual(stop.returncode,125,stop.stderr)
                    self.assertEqual(len(list(partial.glob('step-*.hex'))),1)
                    self.assertEqual([int(s,16) for s in (partial/'step-00.hex').read_text().splitlines()],expected[0])
                    stalled=subprocess.run([str(build/'observer'),'+STALL=1','+OBSERVER_SECONDS=1'],cwd=partial,capture_output=True,text=True,timeout=10)
                    self.assertEqual(stalled.returncode,124,stalled.stderr)
            self.assertTrue(all(item==outputs[0] for item in outputs))
            return outputs[0]
