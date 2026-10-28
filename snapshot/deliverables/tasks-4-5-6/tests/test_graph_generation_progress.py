"""Observer must preserve public results and fail honestly on unfinished runs."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from scripts.comparison.graph_rtl_runtime import emit_generation_testbench


class GenerationProgressTests(unittest.TestCase):
    def test_observer_preserves_completed_public_results(self):
        source=Path(__file__).resolve().parents[1]/'scripts/comparison/graph_generation_progress_main.cpp'
        self.assertTrue(source.exists(),'separate progress observer missing')
        verilator=os.environ.get('LLM2FPGA_VERILATOR') or shutil.which('verilator')
        self.assertIsNotNone(verilator,'Verilator is required for observer equivalence smoke test')
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);(root/'tb.sv').write_text(emit_generation_testbench(65))
            ports=['input clk,reset,go','output done']
            assignments=['reg stall=0;initial if($value$plusargs("STALL=%d",stall))begin end',
                         'reg[7:0]step=0;always @(posedge clk) if(reset)step<=0;else if(go&&!stall&&step<84)step<=step+1;',
                         'assign done=step==84;']
            for role,bits in [('prompt',2),('generated_tokens',4),('last_logits',7)]:
                ports += [f'output [{bits-1}:0] {role}_addr0',
                          f'output {role}_clk,{role}_reset,{role}_content_en,{role}_write_en',
                          f'output[63:0]{role}_write_data',f'input[63:0]{role}_read_data',f'input {role}_done']
                condition={'prompt':"1'b0",'generated_tokens':'step<16','last_logits':'step>=16&&step<81'}[role]
                address={'prompt':'0','generated_tokens':"4'(step)",'last_logits':"7'(step-16)"}[role]
                assignments += [f'assign {role}_clk=clk;',f'assign {role}_reset=reset;',
                    f'assign {role}_content_en=go&&({condition});',f'assign {role}_write_en=go&&({condition});',
                    f'assign {role}_addr0={address};',f'assign {role}_write_data={{56\'d0,step}};']
            (root/'main.sv').write_text('module main('+','.join(ports)+');\n'+'\n'.join(assignments)+'\nendmodule\n')
            (root/'prompt.hex').write_text('1\n2\n3\n4\n')
            for name,driver in [('baseline',None),('observer',source)]:
                args=[verilator,'--cc','--exe','--build','--timing','--assert','--top-module','tb',
                      '-Wno-fatal','--Mdir',str(root/name),'-o',name,'-j','1']
                args += ['--main'] if driver is None else [str(driver)]
                result=subprocess.run(args+[str(root/'tb.sv'),str(root/'main.sv')],
                    capture_output=True,text=True,timeout=180)
                self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            baseline=subprocess.run([str(root/'baseline/baseline')],cwd=root,capture_output=True,text=True,timeout=30)
            observer=subprocess.run([str(root/'observer/observer'),'+OBSERVER_EVERY_EVALS=1'],
                                    cwd=root,capture_output=True,text=True,timeout=30)
            self.assertEqual(baseline.returncode,0,baseline.stderr)
            self.assertEqual(observer.returncode,0,observer.stderr)
            observations=lambda text:[line for line in text.splitlines() if line.startswith(('TOKEN ','LOGIT ','CYCLES '))]
            self.assertEqual(observations(observer.stdout),observations(baseline.stdout))
            self.assertIn('PROGRESS ',observer.stderr)
            self.assertIn('tokens_mask=ffff logits_written=65',observer.stderr)
            stalled=subprocess.run([str(root/'observer/observer'),'+STALL=1','+OBSERVER_SECONDS=1'],
                                   cwd=root,capture_output=True,text=True,timeout=10)
            self.assertEqual(stalled.returncode,124,stalled.stdout+stalled.stderr)
            self.assertIn('OBSERVER unfinished',stalled.stderr)
            self.assertFalse(observations(stalled.stdout))
            for option in ['+OBSERVER_SECONDS=0','+OBSERVER_SECONDS=oops','+OBSERVER_EVERY_EVALS=0']:
                invalid=subprocess.run([str(root/'observer/observer'),option],
                                       cwd=root,capture_output=True,text=True,timeout=10)
                self.assertEqual(invalid.returncode,2,invalid.stderr)

    def test_step_capture_preserves_values_and_diagnostic_stop_is_unfinished(self):
        # Missing/reordered/truncated snapshots or treating a step limit as
        # complete generation must fail, even when final token values look right.
        source=Path(__file__).resolve().parents[1]/'scripts/comparison/graph_generation_progress_main.cpp'
        verilator=os.environ.get('LLM2FPGA_VERILATOR') or shutil.which('verilator')
        self.assertIsNotNone(verilator)
        with tempfile.TemporaryDirectory() as work:
            root=Path(work);(root/'tb.sv').write_text(emit_generation_testbench(65))
            ports=['input clk,reset,go','output done']
            assignments=['integer phase=0;reg premature=0,corrupt=0;',
                'initial begin if($value$plusargs("PREMATURE=%d",premature))begin end if($value$plusargs("CORRUPT=%d",corrupt))begin end end',
                'always @(posedge clk) if(reset)phase<=0;else if(go&&phase<1073)phase<=phase+1;',
                'assign done=phase==1073;']
            for role,bits in [('prompt',2),('generated_tokens',4),('last_logits',7)]:
                ports += [f'output [{bits-1}:0] {role}_addr0',
                    f'output {role}_clk,{role}_reset,{role}_content_en,{role}_write_en',
                    f'output[63:0]{role}_write_data',f'input[63:0]{role}_read_data',f'input {role}_done']
                condition={'prompt':"1'b0",'generated_tokens':'phase<1072&&(phase%67>=65||premature)',
                           'last_logits':'phase<1072&&phase%67<65&&!premature'}[role]
                address={'prompt':'0','generated_tokens':"4'(phase/67)",'last_logits':"7'(phase%67)"}[role]
                data={'prompt':"64'd0",'generated_tokens':"64'(phase/67+(corrupt&&phase%67==66))",
                      'last_logits':"64'(1000*(phase/67)+phase%67)"}[role]
                assignments += [f'assign {role}_clk=clk;',f'assign {role}_reset=reset;',
                    f'assign {role}_content_en=go&&({condition});',f'assign {role}_write_en=go&&({condition});',
                    f'assign {role}_addr0={address};',f'assign {role}_write_data={data};']
            (root/'main.sv').write_text('module main('+','.join(ports)+');\n'+'\n'.join(assignments)+'\nendmodule\n')
            build=root/'build'
            result=subprocess.run([verilator,'--cc','--exe','--build','--timing','--assert',
                '--top-module','tb','-Wno-fatal','--Mdir',str(build),'-o','observer','-j','1',
                str(source),str(root/'tb.sv'),str(root/'main.sv')],capture_output=True,text=True,timeout=180)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            def run(name,*options):
                directory=root/name;directory.mkdir();(directory/'prompt.hex').write_text('1\n2\n3\n4\n')
                return directory,subprocess.run([str(build/'observer'),*options],cwd=directory,
                    capture_output=True,text=True,timeout=30)
            complete,out=run('complete','+OBSERVER_CAPTURE_STEPS=1')
            self.assertEqual(out.returncode,0,out.stdout+out.stderr)
            self.assertEqual(len(list(complete.glob('step-*.hex'))),16)
            for step in range(16):
                words=[int(line,16) for line in (complete/f'step-{step:02d}.hex').read_text().splitlines()]
                self.assertEqual(words,[1000*step+i for i in range(65)])
            saved=(complete/'step-00.hex').read_bytes()
            repeated=subprocess.run([str(build/'observer'),'+OBSERVER_CAPTURE_STEPS=1'],cwd=complete,
                capture_output=True,text=True,timeout=30)
            self.assertEqual(repeated.returncode,2,repeated.stdout+repeated.stderr)
            self.assertEqual((complete/'step-00.hex').read_bytes(),saved)
            partial,out=run('partial','+OBSERVER_CAPTURE_STEPS=1','+OBSERVER_STOP_AFTER_STEPS=1')
            self.assertEqual(out.returncode,125,out.stdout+out.stderr)
            self.assertEqual([p.name for p in partial.glob('step-*.hex')],['step-00.hex'])
            self.assertIn('diagnostic step limit',out.stderr)
            _,out=run('premature','+OBSERVER_CAPTURE_STEPS=1','+PREMATURE=1')
            self.assertEqual(out.returncode,2,out.stdout+out.stderr)
            self.assertIn('before complete logits',out.stderr)
            _,out=run('corrupt','+OBSERVER_CAPTURE_STEPS=1','+CORRUPT=1')
            self.assertEqual(out.returncode,2,out.stdout+out.stderr)
            self.assertIn('completed token changed',out.stderr)
            for i,option in enumerate(('+OBSERVER_CAPTURE_STEPS=2','+OBSERVER_STOP_AFTER_STEPS=17',
                                      '+OBSERVER_STOP_AFTER_STEPS=1')):
                _,out=run(f'invalid-{i}',option)
                self.assertEqual(out.returncode,2,out.stdout+out.stderr)
