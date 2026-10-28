"""Late trusted condition-input lifting under an explicit binary-control contract."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


def iverilog():
    return os.environ.get('LLM2FPGA_IVERILOG', shutil.which('iverilog'))


def vvp():
    return str(Path(iverilog()).with_name('vvp'))


def fixture(*, observer=False, guarded=False, marker='', invoke=False,
            metadata=False, spoof=False, static=False, unrelated_ref=False,
            binary_import='primitives/binary_operators.futil',
            reference_invoke=False, invoke_cg=False, invoke_observer=False,
            invoke_writer=False, mixed=False):
    imports='import "primitives/core.futil";\n'
    imports += ('''comb primitive std_slt<"share"=1>[WIDTH](left: WIDTH, right: WIDTH) -> (out: 1) {
      assign out = 1'b0;
    }\n''' if spoof else f'import "{binary_import}";\n')
    helper='''component helper() -> () { cells { ref p = std_slt(64); } wires {} control {} }\n''' if invoke else ''
    if invoke_writer:helper+='component producer() -> (value: 64) { cells {} wires { value = 64\'d0; } control {} }\n'
    extra_port=', observed: 1' if observer else ''
    extra_cell=('worker = helper();' if invoke else '')+('ref unrelated = std_reg(64);' if unrelated_ref or reference_invoke else '')
    if invoke_observer:extra_cell+='seen = std_reg(1);'
    if invoke_writer:extra_cell+='writer = producer();'
    extra_wire='observed = lt_a.out;' if observer else ''
    if mixed:extra_wire+="lt_a.right = 64'd0;"
    ca_right='' if mixed else "lt_a.right = 64'd0;"
    extra_group="static<1> group unused { a.in = 64'd0; a.write_en = 1'd1; }" if static else ''
    left="gate.out ? a.out" if guarded else 'a.out'
    tail='invoke worker[p = lt_a]()();' if invoke else ''
    if reference_invoke:tail+='invoke unrelated(in = value)();'
    if invoke_cg:tail+='invoke output_reg(in = value)() with ca;'
    if invoke_observer:tail+='invoke seen(in = lt_a.out)();'
    if invoke_writer:tail+='invoke writer()(value = lt_a.left);'
    meta='metadata #{ opaque observer }#' if metadata else ''
    return imports+helper+f'''component main(value: 64) -> (out0: 64{extra_port}) {{
      cells {{
        a = std_reg(64); b = std_reg(64); result = std_reg(64); output_reg = std_reg(64); index = std_reg(64);
        {marker} lt_a = std_slt(64); lt_b = std_slt(64); lt_i = std_slt(64);
        inv = std_not(64); gate = std_slice(64, 1);
        add_three = std_add(64); add_five = std_add(64); add_one = std_add(64); add_index = std_add(64); final_add = std_add(64);
        {extra_cell}
      }}
      wires {{
        out0 = output_reg.out; inv.in = value; gate.in = value; {extra_wire}
        group load_a {{ a.in = value; a.write_en = 1'd1; load_a[done] = a.done; }}
        group load_b {{ b.in = inv.out; b.write_en = 1'd1; load_b[done] = b.done; }}
        group first_neg {{ result.in = 64'd17; result.write_en = 1'd1; first_neg[done] = result.done; }}
        group first_pos {{ result.in = 64'd29; result.write_en = 1'd1; first_pos[done] = result.done; }}
        comb group ca {{ lt_a.left = {left}; {ca_right} }}
        comb group cb {{ lt_b.left = b.out; lt_b.right = 64'd0; }}
        group plus_three {{ add_three.left = result.out; add_three.right = 64'd3; result.in = add_three.out; result.write_en = 1'd1; plus_three[done] = result.done; }}
        group plus_five {{ add_five.left = result.out; add_five.right = 64'd5; result.in = add_five.out; result.write_en = 1'd1; plus_five[done] = result.done; }}
        group init_index {{ index.in = 64'd0; index.write_en = 1'd1; init_index[done] = index.done; }}
        comb group ci {{ lt_i.left = index.out; lt_i.right = 64'd3; }}
        group step {{
          add_one.left = result.out; add_one.right = 64'd1;
          result.in = add_one.out; result.write_en = 1'd1;
          add_index.left = index.out; add_index.right = 64'd1;
          index.in = add_index.out; index.write_en = 1'd1;
          step[done] = result.done & index.done ? 1'd1;
        }}
        group finish {{ final_add.left = value; final_add.right = result.out;
          output_reg.in = final_add.out; output_reg.write_en = 1'd1; finish[done] = output_reg.done; }}
        {extra_group}
      }}
      control {{ seq {{
        load_a; if lt_a.out with ca {{ first_neg; }} else {{ first_pos; }}
        load_b; if lt_b.out with cb {{ plus_three; }} else {{ plus_five; }}
        init_index; while lt_i.out with ci {{ step; }} finish; {tail}
      }} }}
    }}\n'''+meta


class LateCombInputTests(unittest.TestCase):
    def run_tool(self, source, *, enabled=True, none=False):
        tool=Path(os.environ['LLM2FPGA_CALYX_TOOL'])
        args=[str(tool),'-b','calyx','-l',str(tool.parent.parent/'share/calyx'),
              '-p','none' if none else 'late-comb-inputs']
        if enabled and not none:args+=['-x','late-comb-inputs:assume-binary-control=true']
        result=subprocess.run(args,input=source,text=True,capture_output=True,timeout=30)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout

    def group(self, text, name):
        match=re.search(r'comb group '+name+r'\s*\{(.*?)\}',text,re.S)
        self.assertIsNotNone(match,text)
        return match.group(1)

    def test_explicit_contract_lifts_conditions_without_changing_registers(self):
        source=fixture();before=self.run_tool(source,none=True);after=self.run_tool(source)
        self.assertIn('lt_a.left',self.group(before,'ca'))
        self.assertNotIn('lt_a.left',self.group(after,'ca'))
        self.assertNotIn('lt_b.left',self.group(after,'cb'))
        self.assertNotIn('lt_i.left',self.group(after,'ci'))
        self.assertEqual(before.count('= std_reg('),after.count('= std_reg('))
        self.assertIn('if lt_a.out with ca',after)
        self.assertIn('while lt_i.out with ci',after)
        self.assertEqual(self.run_tool(after),after)

    def test_default_contract_is_off(self):
        source=fixture()
        self.assertEqual(self.run_tool(source,enabled=False),self.run_tool(source,none=True))

    def test_public_observer_and_guarded_input_remain_in_group(self):
        for source in [fixture(observer=True),fixture(guarded=True),fixture(marker='@external'),fixture(marker='@protected(1)'),fixture(marker='ref')]:
            with self.subTest(source=source[:90]):
                self.assertIn('lt_a.left',self.group(self.run_tool(source),'ca'))

    def test_invokes_static_groups_and_opaque_metadata_are_conservative(self):
        for source in [fixture(invoke=True),fixture(static=True),fixture(metadata=True)]:
            with self.subTest(source=source[:90]):
                before=self.run_tool(source,none=True)
                self.assertEqual(self.run_tool(source),before)

    def test_standard_name_does_not_authenticate_custom_primitive(self):
        text=self.run_tool(fixture(spoof=True))
        self.assertIn('lt_a.left',self.group(text,'ca'))
        self.assertIn('lt_b.left',self.group(text,'cb'))

    def test_unrelated_reference_does_not_expose_private_comparator(self):
        for source in [fixture(unrelated_ref=True),fixture(reference_invoke=True)]:
            self.assertNotIn('lt_a.left',self.group(self.run_tool(source),'ca'))

    def test_invoke_observation_and_associated_group_block_lifting_locally(self):
        for source in [fixture(invoke_cg=True),fixture(invoke_observer=True),fixture(invoke_writer=True)]:
            text=self.run_tool(source)
            self.assertIn('lt_a.left',self.group(text,'ca'))
            self.assertNotIn('lt_b.left',self.group(text,'cb'))

    def test_continuous_input_stays_unchanged_when_other_input_is_lifted(self):
        text=self.run_tool(fixture(mixed=True))
        self.assertNotIn('lt_a.left',self.group(text,'ca'))
        self.assertEqual(text.count("lt_a.right = 64'd0;"),1)

    def test_changed_extern_bytes_are_not_trusted_despite_identical_declarations(self):
        library=Path(os.environ['LLM2FPGA_CALYX_TOOL']).parent.parent/'share/calyx/primitives'
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            shutil.copyfile(library/'binary_operators.futil',root/'binary_operators.futil')
            original=(library/'binary_operators.sv').read_bytes()
            changed=bytearray(original)
            # Same-length adversarial input library: declaration and size agree.
            position=original.index(b'$signed(left < right)')+len(b'$signed(left ')
            changed[position]=ord('>')
            self.assertEqual(len(changed),len(original))
            (root/'binary_operators.sv').write_bytes(changed)
            text=self.run_tool(fixture(binary_import=str(root/'binary_operators.futil')))
            self.assertIn('lt_a.left',self.group(text,'ca'))

    def test_inline_primitive_body_is_authenticated_independently(self):
        library=Path(os.environ['LLM2FPGA_CALYX_TOOL']).parent.parent/'share/calyx/primitives'
        original=(library/'compile.futil').read_bytes()
        for modified in [False,True]:
            with self.subTest(modified=modified),tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);data=bytearray(original)
                if modified:data[original.index(b'left + right')+len(b'left ')]=ord('-')
                source=root/'compile.futil';source.write_bytes(data)
                program=f'''import "{source}";
                  component main(value:1)->(out:1) {{ cells {{ plus=std_add(1); }}
                  wires {{ out=1'd0; comb group c {{ plus.left=value;plus.right=1'd0; }} }}
                  control {{ if plus.out with c {{ }} else {{ }} }} }}'''
                text=self.run_tool(program)
                if modified:self.assertIn('plus.left',self.group(text,'c'))
                else:self.assertNotIn('plus.left',self.group(text,'c'))

    def test_complete_pipeline_preserves_cpu_results_and_public_cycle_traces(self):
        tool=Path(os.environ['LLM2FPGA_CALYX_TOOL']);library=tool.parent.parent/'share/calyx'
        # Explicit test settings, also runnable in the native backend sandbox.
        resource_passes=('-x','cell-share:bounds=1,-1,-1',
            '-x','cell-share:retire-dead-cells=true',
            '-x','simplify-with-control:retire-dead-groups=true',
            '-x','static-promotion:retire-dead-groups=true')
        seeds=[0,1,-1,(1<<63)-1,-(1<<63),0x123456788765FEDC]
        traces=[];register_counts=[]
        for enabled in [False,True]:
            with tempfile.TemporaryDirectory() as temporary:
                root=Path(temporary);(root/'source.futil').write_text(fixture())
                args=[str(tool),'source.futil','-p','external',*resource_passes,'--synthesis','--nested','-l',str(library)]
                if enabled:args+=['-x','late-comb-inputs:assume-binary-control=true']
                # Prove the full-flow test is non-vacuous after register-call
                # conversion and sharing, before condition capture consumes CGs.
                prefix=['validate','synthesis-papercut','infer-data-path','collapse-control',
                    'compile-sync-without-sync-reg','group2seq','dead-assign-removal','group2invoke',
                    'infer-share','inline','comb-prop','constant-port-prop','dead-cell-removal',
                    'cell-share','late-comb-inputs']
                inspect=[str(tool),'source.futil','-b','calyx',*resource_passes,'--synthesis','-l',str(library)]
                if enabled:inspect+=['-x','late-comb-inputs:assume-binary-control=true']
                for name in prefix:inspect+=['-p',name]
                result=subprocess.run(inspect,cwd=root,text=True,capture_output=True,timeout=60)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn('invoke ',result.stdout,'fixture must cover native register invokes')
                ca=self.group(result.stdout,'ca')
                if enabled:self.assertNotIn('lt_a.left',ca,'CPU/RTL test must exercise actual lifting')
                else:self.assertIn('lt_a.left',ca)
                for backend,output in [('calyx','compiled.futil'),('verilog','kernel.sv')]:
                    result=subprocess.run([*args,'-b',backend,'-o',output],cwd=root,text=True,capture_output=True,timeout=90)
                    self.assertEqual(result.returncode,0,result.stderr)
                register_counts.append((root/'compiled.futil').read_text().count('= std_reg('))
                checks=[]
                for seed in seeds:
                    bits=seed&((1<<64)-1);signed=bits if bits<(1<<63) else bits-(1<<64)
                    expected=(bits+(25 if signed<0 else 35))&((1<<64)-1)
                    checks.append(f'''reset=1;go=0;repeat(3)@(negedge clk);value=64'h{bits:x};reset=0;go=1;cycles=0;
                      while(!done&&cycles<300)begin @(negedge clk);cycles=cycles+1;end
                      if(!done||out0!==64'h{expected:x})$fatal(1,"CPU mismatch input=%h output=%h",value,out0);
                      go=0;repeat(3)@(negedge clk);''')
                (root/'tb.sv').write_text('''module tb;reg clk=0,reset=1,go=0;reg[63:0]value=0;
                  wire done;wire[63:0]out0;integer cycles=0;main dut(.*);always #5 clk=~clk;
                  always @(negedge clk)$display("TRACE %h %b%b%b %h",value,reset,go,done,out0);
                  initial begin '''+''.join(checks)+''' $display("PASS CPU");$finish;end endmodule''')
                build=subprocess.run([iverilog(),'-g2012','-s','tb','-o','sim','tb.sv','kernel.sv'],cwd=root,text=True,capture_output=True,timeout=60)
                self.assertEqual(build.returncode,0,build.stderr)
                sim=subprocess.run([vvp(),'sim'],cwd=root,text=True,capture_output=True,timeout=60)
                self.assertEqual(sim.returncode,0,sim.stdout+sim.stderr)
                self.assertIn('PASS CPU',sim.stdout)
                traces.append([l for l in sim.stdout.splitlines() if l.startswith('TRACE ')])
        self.assertEqual(register_counts[0],register_counts[1])
        self.assertEqual(traces[0],traces[1])

    def test_x_activation_is_outside_the_explicit_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'tb.sv').write_text('''module tb;reg g=1'bx;wire a,b;
              assign a=g?~(g?1'b1:1'b0):1'b0;
              assign b=g?~1'b1:1'b0;
              initial begin #1;if(a!==1'bx||b!==1'b0)$fatal(1,"expected X-control distinction");
                $display("PASS explicit X limit");$finish;end endmodule''')
            build=subprocess.run([iverilog(),'-g2012','-o','sim','tb.sv'],cwd=root,text=True,capture_output=True,timeout=30)
            self.assertEqual(build.returncode,0,build.stderr)
            sim=subprocess.run([vvp(),'sim'],cwd=root,text=True,capture_output=True,timeout=30)
            self.assertEqual(sim.returncode,0,sim.stdout+sim.stderr)
            self.assertIn('PASS explicit X limit',sim.stdout)


if __name__=='__main__':unittest.main()
