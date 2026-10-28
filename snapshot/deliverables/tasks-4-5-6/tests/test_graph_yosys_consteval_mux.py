"""Native constant evaluation preserves mux values and bounded FSM scaling."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


class ConstEvalMuxTests(unittest.TestCase):
    def test_selector_and_unresolved_data_records_match_baseline(self):
        candidate = Path(shutil.which(os.environ.get('YOSYS', 'yosys'))).resolve()
        baseline = Path(os.environ['LLM2FPGA_YOSYS_BASELINE']).resolve()
        source = Path(__file__).with_name('yosys_consteval_mux_probe.cpp')
        records = []
        with tempfile.TemporaryDirectory() as temporary:
            for label, tool in [('baseline', baseline), ('candidate', candidate)]:
                config = tool.with_name('yosys-config')
                flags = shlex.split(subprocess.check_output([str(config), '--cxxflags'], text=True))
                plugin = Path(temporary) / (label + '.so')
                subprocess.run(['g++', *flags, '-shared', '-Wl,-Bsymbolic', '-o',
                                str(plugin), str(source)], check=True, capture_output=True, timeout=60)
                result = subprocess.run([str(tool), '-Q', '-T', '-m', str(plugin),
                                         '-p', 'consteval_probe'], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                selected = [line for line in result.stdout.splitlines() if line.startswith('PROBE')]
                self.assertEqual(selected[-1], 'PROBE_CASES 1060')
                records.append(selected)
        self.assertEqual(*records)

    def test_per_state_advance_fsm_extracts_within_guard(self):
        count, width = 512, 9
        lines = [f'module main(input clk,reset,input[{count-1}:0] advance,output[{count-1}:0] decoded);',
                 f'reg[{width-1}:0] state_reg;reg[{width-1}:0] state_next;',
                 'always @(posedge clk) if(reset)state_reg<=0;else state_reg<=state_next;',
                 'always @* begin state_next=0;case(state_reg)']
        lines += [f"{width}'d{i}:state_next=advance[{i}] ? {width}'d{(i+1)%count} : {width}'d{i};"
                  for i in range(count)]
        lines += ['endcase end', f'genvar i;generate for(i=0;i<{count};i=i+1)begin '
                  'assign decoded[i]=(state_reg==i);end endgenerate', 'endmodule']
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'fsm.sv'
            source.write_text('\n'.join(lines) + '\n')
            script = f'read_verilog -sv {source}; proc; opt -nodffe -nosdff; fsm_detect; fsm_extract; stat'
            result = subprocess.run([os.environ.get('YOSYS', 'yosys'), '-Q', '-T', '-p', script],
                                    capture_output=True, text=True, timeout=4)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Extracting FSM', result.stdout)
