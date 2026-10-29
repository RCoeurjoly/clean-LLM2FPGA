"""Native constant evaluation preserves mux values."""
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
