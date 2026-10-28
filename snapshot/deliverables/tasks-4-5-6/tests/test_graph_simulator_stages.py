"""Simulator compilation must wait until native emission has exited."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

class SimulatorStagesTests(unittest.TestCase):
    def test_native_emission_exits_before_cpp_compilation(self):
        script=Path(__file__).resolve().parents[1]/'scripts/comparison/build_graph_generation_simulator.py'
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);rtl=root/'kernel.sv';rtl.write_text('module main; endmodule')
            native=root/'native';native.write_text('''#!/usr/bin/env python3
import os,sys
from pathlib import Path
assert all(flag in sys.argv for flag in ('--cc','--exe','--main'))
assert '--binary' not in sys.argv and '--build' not in sys.argv
out=Path(sys.argv[sys.argv.index('--Mdir')+1]);out.mkdir(parents=True)
(out/'emitter.pid').write_text(str(os.getpid()))
(out/'Vtb.cpp').write_text('// synthetic simulator fixture')
(out/'Vtb.mk').write_text('# synthetic make fixture')
''');native.chmod(0o755)
            make=root/'make';make.write_text('''#!/usr/bin/env python3
import os
from pathlib import Path
out=Path('verilator');pid=int((out/'emitter.pid').read_text())
try:os.kill(pid,0)
except ProcessLookupError:pass
else:raise RuntimeError('native emitter still alive')
(out/'simulator').write_text('synthetic binary fixture')
''');make.chmod(0o755)
            result=subprocess.run([sys.executable,str(script),'--rtl',str(rtl),'--output',str(root/'build'),'--verilator',str(native),'--make',str(make),'--logit-words','3'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertTrue((root/'build/emission/success.json').is_file())
            self.assertTrue((root/'build/success.json').is_file())
