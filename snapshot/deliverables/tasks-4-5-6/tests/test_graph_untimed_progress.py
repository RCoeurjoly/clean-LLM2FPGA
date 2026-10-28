"""External clock edges must preserve timed-bench reset and public transactions."""
import importlib.util
from pathlib import Path
import unittest
import test_graph_threaded_progress as fixture
from scripts.comparison.graph_rtl_runtime import emit_generation_testbench

EVIDENCE=Path(__file__).resolve().parents[3]/'artifacts/task6/full-model-numerical-2026-10-24'


@unittest.skipUnless(EVIDENCE.is_dir(),'requires retained goal study')
class UntimedProgressTests(unittest.TestCase):
    def test_clock_input_matches_timed_bench_and_literal_reset_sensitive_values(self):
        source=EVIDENCE/'untimed_runtime.py';driver=EVIDENCE/'untimed_progress_main.cpp'
        self.assertTrue(source.is_file(),'clock-input testbench missing')
        self.assertTrue(driver.is_file(),'clock-input driver missing')
        spec=importlib.util.spec_from_file_location('untimed_runtime',source)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        timed=fixture.ThreadedProgressTests.exercise_lanes(self,emit_generation_testbench,
            EVIDENCE/'parallel_progress_main.cpp',thread_counts=(1,))
        untimed=fixture.ThreadedProgressTests.exercise_lanes(self,module.emit_generation_testbench,
            driver,timing=False,thread_counts=(1,))
        self.assertEqual(timed,untimed)
