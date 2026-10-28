"""Per-step acceptance must reject plausible tokens paired with wrong logits."""
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import hashlib


class ObservedGenerationTests(unittest.TestCase):
    references=[(1,[-1,3,3,0]),(2,[-(1<<63),0,(1<<63)-1,-2])]

    def fixture(self,root):
        # Independent literal bit patterns include a winning tie and both
        # signed extremes. No compiler/reference helper computes these files.
        (root/'step-00.hex').write_text('ffffffffffffffff\n0000000000000003\n0000000000000003\n0000000000000000\n')
        (root/'step-01.hex').write_text('8000000000000000\n0000000000000000\n7fffffffffffffff\nfffffffffffffffe\n')
        return 'STEP 0 0000000000000001 4 step-00.hex\nSTEP 1 0000000000000002 4 step-01.hex\n'

    def compare(self,root,stdout):
        from scripts.comparison.verify_graph_generation_observed import compare_observations
        return compare_observations(root,stdout,self.references,logit_words=4)

    def test_exact_full_width_steps_and_earliest_winning_tie(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);out=self.fixture(root)
            result=self.compare(root,out)
            self.assertEqual([r['token'] for r in result],[1,2])
            self.assertEqual([r['logit_words_equal'] for r in result],[4,4])

    def test_wrong_logits_are_rejected_even_when_tokens_match(self):
        for change in ('one_word','stale_previous_step','truncated','extra_word','unknown_bits','symlink'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);out=self.fixture(root);p=root/'step-01.hex'
                if change=='one_word':p.write_text('8000000000000001\n0000000000000000\n7fffffffffffffff\nfffffffffffffffe\n')
                elif change=='stale_previous_step':p.write_bytes((root/'step-00.hex').read_bytes())
                elif change=='truncated':p.write_text('8000000000000000\n')
                elif change=='extra_word':p.write_text(p.read_text()+'0000000000000000\n')
                elif change=='unknown_bits':p.write_text('xxxxxxxxxxxxxxxx\n'*4)
                else:p.unlink();p.symlink_to(root/'step-00.hex')
                with self.assertRaises(ValueError):self.compare(root,out)

    def test_missing_duplicate_reordered_or_forged_step_records_are_rejected(self):
        for change in ('missing','duplicate','reordered','wrong_token','wrong_geometry','wrong_filename'):
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);out=self.fixture(root);a,b=out.splitlines(keepends=True)
                out={'missing':a,'duplicate':a+a+b,'reordered':b+a,
                    'wrong_token':a.replace('0000000000000001','0000000000000002')+b,
                    'wrong_geometry':a.replace(' 4 ',' 3 ')+b,
                    'wrong_filename':a.replace('step-00.hex','../step-00.hex')+b}[change]
                with self.assertRaises(ValueError):self.compare(root,out)

    def test_only_explicit_step_limit_can_validate_a_partial_generation(self):
        from scripts.comparison.verify_graph_generation_observed import validate_observer_exit
        validate_observer_exit(125,'OBSERVER diagnostic step limit; generation unfinished',steps=1)
        validate_observer_exit(0,'',steps=16)
        for code,err,steps in ((124,'OBSERVER unfinished',1),(0,'',1),(125,'',1),
                               (125,'OBSERVER diagnostic step limit; generation unfinished',16)):
            with self.subTest(code=code,steps=steps),self.assertRaises(ValueError):
                validate_observer_exit(code,err,steps=steps)

    def test_stdout_diagnostics_cannot_validate_otherwise_matching_steps(self):
        from scripts.comparison.verify_graph_generation_observed import validate_observer_exit
        for diagnostic in ('%Warning: input is missing','$readmem file not found','%Error: bad memory'):
            with self.subTest(diagnostic=diagnostic),self.assertRaises(ValueError):
                validate_observer_exit(125,'OBSERVER diagnostic step limit; generation unfinished',
                                       steps=1,stdout=diagnostic)

    def test_runtime_inputs_changed_by_an_otherwise_successful_process_are_rejected(self):
        from scripts.comparison.verify_graph_generation_observed import verify_runtime_inputs
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);out=self.fixture(root)
            for filename in ('prompt.hex','input-0000.hex'):(root/filename).write_bytes(b'original\n')
            expected={name:hashlib.sha256(b'original\n').hexdigest() for name in ('prompt.hex','input-0000.hex')}
            before=verify_runtime_inputs(root,expected)
            self.assertEqual(len(before),2)
            for filename in expected:
                with self.subTest(filename=filename):
                    process=subprocess.run([sys.executable,'-c',
                        'from pathlib import Path;Path('+repr(str(root/filename))+').write_bytes(b"changed\\n");print('+repr(out)+')'],
                        capture_output=True,text=True,check=True)
                    self.compare(root,process.stdout) # Outputs still look right.
                    with self.assertRaises(ValueError):verify_runtime_inputs(root,expected)
                    (root/filename).write_bytes(b'original\n')

    def test_live_reference_code_must_match_frozen_implementation(self):
        from scripts.comparison.verify_graph_generation_observed import verify_reference_sources
        names=('compiler/frontend/package_inputs.py','compiler/frontend/package_model.py',
               'compiler/frontend/fixed_point_ops.py','compiler/generation.py','TinyStories/serial_gemv_boundary.py')
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);live=root/'live';frozen=root/'frozen';identities={}
            for name in names:
                for base in (live,frozen):
                    p=base/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'frozen implementation\n')
                identities[str(frozen/name)]=hashlib.sha256(b'frozen implementation\n').hexdigest()
            self.assertEqual(len(verify_reference_sources(live,identities)),5)
            for name in names:
                with self.subTest(name=name):
                    (live/name).write_bytes(b'changed implementation\n')
                    with self.assertRaises(ValueError):verify_reference_sources(live,identities)
                    (live/name).write_bytes(b'frozen implementation\n')

    def test_goal_scope_requires_both_complete_prompt_cases(self):
        from scripts.comparison.verify_graph_generation_observed import full_goal_scope_validated
        a={'case':'sequential','generation_complete':True};b={'case':'alternate','generation_complete':True}
        self.assertFalse(full_goal_scope_validated([a]))
        self.assertTrue(full_goal_scope_validated([a,b]))
        self.assertFalse(full_goal_scope_validated([a,a]))
        self.assertFalse(full_goal_scope_validated([a,{**b,'generation_complete':False}]))

    def test_failed_preflight_invalidates_report_and_preserves_input_evidence(self):
        script=Path(__file__).resolve().parents[1]/'scripts/comparison/verify_graph_generation_observed.py'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            argv=[sys.executable,str(script)]
            for role in ('root','binary','snapshot','package','reference-source','build-dir','synthesis-root'):
                argv.extend(('--'+role,str(root/role)))
            report=root/'report.json';report.write_text('{"old_success":true}')
            result=subprocess.run([*argv,'--output',str(report)],capture_output=True,text=True,timeout=30)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse(report.exists())
            for role in ('root','binary','snapshot','package','reference-source','build-dir','synthesis-root'):
                evidence=root/role
                evidence.write_text('preserve evidence')
                result=subprocess.run([*argv,'--output',str(evidence)],capture_output=True,text=True,timeout=30)
                self.assertNotEqual(result.returncode,0)
                self.assertIn('overlap',result.stderr)
                self.assertEqual(evidence.read_text(),'preserve evidence')
                evidence.unlink()
            target=root/'target';target.write_text('preserve target');report.symlink_to(target)
            result=subprocess.run([*argv,'--output',str(report)],capture_output=True,text=True,timeout=30)
            self.assertNotEqual(result.returncode,0)
            self.assertTrue(report.is_symlink())
            self.assertEqual(target.read_text(),'preserve target')
            report.unlink()
            runtime=root/'runtime';evidence=runtime/'sequential/step-00.hex'
            evidence.parent.mkdir(parents=True);evidence.write_text('preserve verified snapshot')
            result=subprocess.run([*argv,'--work-dir',str(runtime),'--output',str(evidence)],
                capture_output=True,text=True,timeout=30)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('overlap',result.stderr)
            self.assertEqual(evidence.read_text(),'preserve verified snapshot')
