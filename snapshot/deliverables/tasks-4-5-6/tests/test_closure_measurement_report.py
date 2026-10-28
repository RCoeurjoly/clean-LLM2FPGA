import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from compiler.measurements import measure_stage
from compiler.stages import digest

class MeasurementReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'input';self.source.write_text('source')
        self.folder=self.root/'observation';self.profile='a'*64
        self.result=measure_stage([sys.executable,'-c','import time;time.sleep(.08)'],self.folder,
            input_hashes={str(self.source):digest(self.source)},tool_hashes={sys.executable:digest(Path(sys.executable))},
            profile_sha256=self.profile,timeout_seconds=2,sample_interval_seconds=.01)
        self.load=importlib.import_module('scripts.pipeline.build_closure_measurement_report').load_measurement
    def mutate(self,**fields):
        q=json.loads((self.folder/'measurement.json').read_text());q.update(fields)
        (self.folder/'measurement.json').write_text(json.dumps(q))
    def test_real_native_observation_reconciles(self):
        q=self.load(self.folder,self.profile)
        self.assertEqual(q['status'],'completed');self.assertFalse(q['build_acceptance'])
        self.assertEqual(q['peak_memory_kib'],self.result['peak_memory_kib'])
    def test_profile_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError,'profile'):self.load(self.folder,'b'*64)
    def test_changed_dependency_rejected(self):
        self.source.write_text('changed')
        with self.assertRaisesRegex(ValueError,'dependency'):self.load(self.folder,self.profile)
    def test_peak_must_reconcile_with_raw_samples(self):
        self.mutate(peak_memory_kib=self.result['peak_memory_kib']+1)
        with self.assertRaisesRegex(ValueError,'sample'):self.load(self.folder,self.profile)
    def test_boolean_and_nonfinite_units_rejected(self):
        for value in [True,float('nan'),-1]:
            with self.subTest(value=value):
                self.mutate(elapsed_seconds=value)
                with self.assertRaises(ValueError):self.load(self.folder,self.profile)
    def test_missing_raw_log_rejected(self):
        (self.folder/'stderr.log').unlink()
        with self.assertRaisesRegex(ValueError,'log'):self.load(self.folder,self.profile)
    def test_completion_requires_zero_exit(self):
        self.mutate(exit_code=1)
        with self.assertRaisesRegex(ValueError,'exit'):self.load(self.folder,self.profile)
    def test_changed_spec_rejected(self):
        p=self.folder/'measurement-spec.json';p.write_text(p.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'spec'):self.load(self.folder,self.profile)
    def test_failed_native_process_retains_observation(self):
        folder=self.root/'failed'
        measure_stage([sys.executable,'-c','import sys,time;time.sleep(.08);sys.exit(7)'],folder,
            input_hashes={str(self.source):digest(self.source)},tool_hashes={sys.executable:digest(Path(sys.executable))},
            profile_sha256=self.profile,timeout_seconds=2,sample_interval_seconds=.01)
        q=self.load(folder,self.profile)
        self.assertEqual(q['status'],'failed');self.assertEqual(q['exit_code'],7)
        self.assertFalse(q['build_acceptance'])
    def test_peak_units_reject_boolean_even_when_numeric_equal(self):
        trace=self.folder/'memory-samples.jsonl'
        rows=[json.loads(line) for line in trace.read_text().splitlines()]
        for row in rows:row['rss_kib']=1
        trace.write_text(''.join(json.dumps(row)+'\n' for row in rows))
        self.mutate(peak_memory_kib=True)
        with self.assertRaises(ValueError):self.load(self.folder,self.profile)
    def test_real_timeout_retains_limits_and_failure(self):
        folder=self.root/'timeout'
        measure_stage([sys.executable,'-c','import time;time.sleep(5)'],folder,
            input_hashes={str(self.source):digest(self.source)},tool_hashes={sys.executable:digest(Path(sys.executable))},
            profile_sha256=self.profile,timeout_seconds=.06,sample_interval_seconds=.01)
        q=self.load(folder,self.profile)
        self.assertEqual(q['status'],'timeout');self.assertEqual(q['limits']['timeout_seconds'],.06)
        self.assertFalse(q['build_acceptance'])
    def test_changed_collector_identity_rejected(self):
        spec=self.folder/'measurement-spec.json';q=json.loads(spec.read_text());q['collector_sha256']='c'*64
        spec.write_text(json.dumps(q));self.mutate(measurement_spec_sha256=digest(spec))
        with self.assertRaisesRegex(ValueError,'collector'):self.load(self.folder,self.profile)
    def test_nonobject_document_rejected_cleanly(self):
        (self.folder/'measurement.json').write_text('[]')
        with self.assertRaises(ValueError):self.load(self.folder,self.profile)


class Xc7ResourceAccountingTests(unittest.TestCase):
    def usage(self, counts):
        module = importlib.import_module('scripts.pipeline.build_closure_measurement_report')
        function = getattr(module, 'xc7_resource_usage', None)
        self.assertTrue(callable(function), 'mapped XC7 resource accounting required')
        return function(counts)

    def test_lutram_is_separate_and_does_not_claim_physical_lut_cost(self):
        usage = self.usage({'LUT6': 2, 'RAM64X1S': 3, 'FDRE': 4,
                            'RAMB18E1': 1, 'RAMB36E1': 2, 'DSP48E1': 5})
        self.assertEqual(usage['ordinary_lut'], 2)
        self.assertEqual(usage['lutram_cells'], {'RAM64X1S': 3})
        self.assertIsNone(usage['lutram_luts'])
        self.assertEqual(usage['registers'], 4)
        self.assertEqual(usage['bram36_equivalent'], 2.5)
        self.assertEqual(usage['dsp'], 5)

    def test_absent_lutram_is_zero(self):
        self.assertEqual(self.usage({'LUT2': 7})['lutram_luts'], 0)

    def test_unmapped_and_unknown_cells_are_rejected(self):
        for name in ('$mul', 'unknown_primitive'):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'primitive'):
                self.usage({name: 1})

    def test_invalid_cell_counts_are_rejected(self):
        for count in (True, -1, 1.5, float('nan')):
            with self.subTest(count=count), self.assertRaises(ValueError):
                self.usage({'LUT6': count})
