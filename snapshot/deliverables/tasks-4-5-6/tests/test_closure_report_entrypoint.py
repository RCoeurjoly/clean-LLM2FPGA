"""Report assembly tests; core authentication has its own native fixture suite."""
import json
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import scripts.pipeline.build_closure_measurement_report as report
from compiler.stages import digest
from compiler.measurements import measure_stage
import sys

class ReportEntryPointTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.baseline=self.root/'baseline';self.baseline.mkdir()
        (self.baseline/'stat.json').write_text(json.dumps({'top_module':'baseline','num_cells_by_type':{'LUT6':9}}))
        (self.baseline/'summary.json').write_text(json.dumps({'usage':{'lut_total':9}}))
        self.output=self.root/'report';self.receipts=[]
        for model,hidden in zip(('1M','3M','8M','28M','33M'),(64,128,256,512,768)):
            config=self.root/(model+'-config.json');config.write_text(json.dumps({'hidden_size':hidden,'num_heads':16,'num_layers':8,'vocab_size':50257}))
            contract=self.root/(model+'-contract.json');contract.write_text(json.dumps({'inputs':{'snapshot':{'config.json':digest(config)}}}))
            profile=SimpleNamespace(identity_sha256=hashlib.sha256(model.encode()).hexdigest(),numerical_contract=contract)
            self.receipts.append({'model':model,'core':self.root/model,'profile':profile,'snapshot_config':config,'measurements':[]})
        self.auth=patch.object(report,'load_core_evidence',side_effect=self.evidence);self.auth.start();self.addCleanup(self.auth.stop)
    def evidence(self,core,profile):
        return {'core_directory':str(core),'core_identity_sha256':'core-'+core.name,'profile_sha256':profile.identity_sha256,'pipeline_identity_sha256':'common','compilation_authenticated':True}
    def build(self):
        fn=getattr(report,'build_report',None);self.assertTrue(callable(fn),'public build_report required')
        return fn(self.receipts,self.baseline,self.output)
    def test_missing_metrics_remain_null_and_never_claim_closure(self):
        result=self.build();self.assertEqual(len(result['rows']),5)
        self.assertEqual(result['status'],'incomplete')
        for row in result['rows']:
            self.assertIsNone(row['whole_build_wall_seconds']);self.assertIsNone(row['routed_fmax_mhz'])
            self.assertIsNone(row['ordinary_lut'])
        for name in ('matrix.json','matrix.csv','fits.json','evidence-index.json','measurements.svg'):
            self.assertTrue((self.output/name).is_file(),name)
        self.assertEqual(result['baseline_hashes']['stat.json'],digest(self.baseline/'stat.json'))
    def test_duplicate_or_missing_family_lane_is_rejected_before_output(self):
        self.receipts[-1]=self.receipts[0]
        with self.assertRaisesRegex(ValueError,'five|unique'):self.build()
        self.assertFalse(self.output.exists())
    def test_mixed_compiler_programs_are_rejected(self):
        original=self.evidence
        def mixed(core,profile):
            row=original(core,profile)
            if core.name=='33M':row['pipeline_identity_sha256']='other'
            return row
        report.load_core_evidence.side_effect=mixed
        with self.assertRaisesRegex(ValueError,'profile|program'):self.build()
        self.assertFalse(self.output.exists())
    def test_changed_model_config_rejected(self):
        self.receipts[0]['snapshot_config'].write_text('{}')
        with self.assertRaisesRegex(ValueError,'config'):self.build()
    def test_existing_output_is_not_overwritten(self):
        self.output.mkdir();(self.output/'user-data').write_text('keep')
        with self.assertRaisesRegex(ValueError,'fresh|exist'):self.build()
        self.assertEqual((self.output/'user-data').read_text(),'keep')
    def test_failure_observation_is_preserved_and_excluded_from_fits(self):
        lane=self.receipts[0];folder=self.root/'failed'
        measure_stage([sys.executable,'-c','import sys,time;time.sleep(.02);sys.exit(7)'],folder,
            input_hashes={str(lane['snapshot_config']):digest(lane['snapshot_config'])},
            tool_hashes={sys.executable:digest(Path(sys.executable))},profile_sha256=lane['profile'].identity_sha256,
            timeout_seconds=2,sample_interval_seconds=.01)
        lane['measurements']=[{'scope':'whole_build','directory':folder}]
        result=self.build();row=result['rows'][0]
        self.assertIsNone(row['whole_build_wall_seconds'])
        self.assertEqual(row['measurement_observations'][0]['exit_code'],7)
        self.assertEqual(row['measurement_observations'][0]['status'],'failed')
        self.assertEqual(result['fits']['whole_build_wall_seconds']['status'],'insufficient completed observations')
    def test_same_profile_measurement_with_changed_input_is_rejected(self):
        lane=self.receipts[0];folder=self.root/'changed'
        measure_stage([sys.executable,'-c','pass'],folder,input_hashes={str(lane['snapshot_config']):digest(lane['snapshot_config'])},tool_hashes={sys.executable:digest(Path(sys.executable))},profile_sha256=lane['profile'].identity_sha256,timeout_seconds=2)
        lane['measurements']=[{'scope':'capture','directory':folder}]
        spec=folder/'measurement-spec.json';spec.write_text(spec.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'spec'):self.build()
        self.assertFalse(self.output.exists())

    def test_partial_native_stage_sum_is_rejected(self):
        lane=self.receipts[0];folder=self.root/'native-partial'
        measure_stage([sys.executable,'-c','pass'],folder,input_hashes={str(lane['snapshot_config']):digest(lane['snapshot_config'])},tool_hashes={sys.executable:digest(Path(sys.executable))},profile_sha256=lane['profile'].identity_sha256,timeout_seconds=2)
        lane['measurements']=[{'scope':'native','directory':folder}]
        with self.assertRaisesRegex(ValueError,'native'):self.build()
        self.assertFalse(self.output.exists())

    def test_completed_whole_build_observations_produce_scoped_trend(self):
        for lane in self.receipts:
            folder=self.root/(lane['model']+'-whole')
            measure_stage([sys.executable,'-c','import time;time.sleep(.03)'],folder,input_hashes={str(lane['snapshot_config']):digest(lane['snapshot_config'])},tool_hashes={sys.executable:digest(Path(sys.executable))},profile_sha256=lane['profile'].identity_sha256,timeout_seconds=2,sample_interval_seconds=.01)
            lane['measurements']=[{'scope':'whole_build','directory':folder}]
        result=self.build()
        self.assertEqual(result['fits']['whole_build_wall_seconds']['sample_count'],5)
        self.assertEqual(result['fits']['whole_build_wall_seconds']['y_unit'],'seconds')
        self.assertFalse(result['fits']['whole_build_wall_seconds']['predictive_acceptance'])
        for row in result['rows']:
            self.assertGreater(row['whole_build_wall_seconds'],0)
            self.assertIsNone(row['separate_native_wall_seconds_sum'])

    def test_native_receipt_may_include_upstream_stage_specs(self):
        upstream=self.root/'upstream-spec.json';executed=self.root/'executed-spec.json'
        upstream.write_text(json.dumps({'argv':['tool','upstream'],'tool_hashes':{'tool':'hash'}}))
        executed.write_text(json.dumps({'argv':['tool','executed'],'tool_hashes':{'tool':'hash'}}))
        measured={'argv':['tool','executed'],'tool_hashes':{'tool':'hash'},'input_hashes':{str(upstream):digest(upstream),str(executed):digest(executed)}}
        fn=getattr(report,'_native_observation_source',None)
        self.assertTrue(callable(fn),'native source identification required')
        self.assertEqual(fn(measured,{str(upstream),str(executed)}),str(executed))

    def test_report_output_cannot_contaminate_durable_baseline(self):
        self.output=self.baseline/'generated'
        before={p.name:digest(p) for p in self.baseline.iterdir()}
        with self.assertRaisesRegex(ValueError,'overlap|baseline'):self.build()
        self.assertFalse(self.output.exists())
        self.assertEqual(before,{p.name:digest(p) for p in self.baseline.iterdir()})

    def test_report_output_cannot_contaminate_authenticated_core(self):
        core=self.receipts[0]['core'];core.mkdir()
        self.output=core/'generated'
        with self.assertRaisesRegex(ValueError,'overlap|input'):self.build()
        self.assertFalse(self.output.exists())

    def test_symlink_alias_cannot_bypass_baseline_isolation(self):
        alias=self.root/'alias';alias.symlink_to(self.baseline,target_is_directory=True)
        self.output=alias/'generated'
        with self.assertRaisesRegex(ValueError,'overlap|baseline'):self.build()
        self.assertFalse(self.output.exists())

    def native_stage_fixture(self, count):
        from compiler.stages import StageSpec,run_stage
        lane=self.receipts[0];core=lane['core'];core.mkdir()
        dependencies={};declarations=[]
        for index in range(count):
            stage=core/('stage-'+str(index));filename='artifact-'+str(index)+'.txt'
            argv=[sys.executable,'-c',"from pathlib import Path;Path("+repr(filename)+").write_text('computed fixture')"]
            inputs={str(lane['snapshot_config']):digest(lane['snapshot_config'])}
            tools={sys.executable:digest(Path(sys.executable))}
            run_stage(StageSpec('fixture-native',tuple(argv),inputs,tools,{'scope':'report assembly fixture'},(filename,)),stage)
            original=stage/'spec.json';dependencies[str(original)]=digest(original)
            dependencies[str(stage/filename)]=digest(stage/filename)
            measured=self.root/('native-observation-'+str(index))
            measure_stage(argv,measured,input_hashes={**inputs,str(original):digest(original)},tool_hashes=tools,profile_sha256=lane['profile'].identity_sha256,timeout_seconds=2,sample_interval_seconds=.01)
            declarations.append({'scope':'native','directory':measured})
        (core/'core.json').write_text(json.dumps({'dependency_hashes':dependencies}))
        return lane,declarations

    def test_complete_native_coverage_uses_declared_stage_set(self):
        lane,declarations=self.native_stage_fixture(1)
        lane['measurements']=declarations
        row=self.build()['rows'][0]
        self.assertGreater(row['separate_native_wall_seconds_sum'],0)
        self.assertEqual(len(row['measurement_observations']),1)
        self.assertIsNone(row['whole_build_wall_seconds'])

    def test_missing_declared_native_stage_is_rejected(self):
        lane,declarations=self.native_stage_fixture(2)
        lane['measurements']=declarations[:1]
        with self.assertRaisesRegex(ValueError,'cover all original core stages'):
            self.build()
        self.assertFalse(self.output.exists())
