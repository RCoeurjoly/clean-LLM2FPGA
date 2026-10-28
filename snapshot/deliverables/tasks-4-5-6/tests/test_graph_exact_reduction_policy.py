"""Selected exact reductions must change authenticated schedules, never silently."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from compiler.pipeline import integer_profile
import test_graph_build_profile
import test_graph_core_cli


class ExactReductionPolicyTests(unittest.TestCase):
    def test_sqrt_selection_is_explicit_and_after_strength_reduction(self):
        flag = '--llm2fpga-recover-restoring-sqrt-loops'
        baseline = integer_profile()
        self.assertFalse(any(flag in stage[2] for stage in baseline))
        selected = integer_profile(recover_restoring_sqrt_loops=True)
        loops = next(stage[2] for stage in selected if stage[0] == 'loops')
        self.assertIn(flag, loops)
        self.assertLess(loops.index('--llm2fpga-strength-reduce-integer-constants'), loops.index(flag))
        self.assertGreater(loops.index('--canonicalize', loops.index(flag)), loops.index(flag))

    def test_invalid_selection_is_rejected_before_input_access(self):
        for value in (1, 'yes', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                integer_profile(recover_restoring_sqrt_loops=value)

    def test_selection_changes_profile_identity_and_declared_native_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = test_graph_build_profile.BuildProfileTests().profile(Path(temporary))
            selected = replace(base, recover_restoring_sqrt_loops=True)
            self.assertNotEqual(base.identity_sha256, selected.identity_sha256)
            self.assertTrue(selected.document()['optimization_policy']['recover_restoring_sqrt_loops'])
            loops = next(s['arguments'] for s in selected.document()['pass_sequence'] if s['name'] == 'loops')
            self.assertIn('--llm2fpga-recover-restoring-sqrt-loops', loops)
            with self.assertRaises(ValueError):
                replace(base, recover_restoring_sqrt_loops=1)

    def test_cli_selects_sqrt_without_changing_default(self):
        from compiler.compile_core import parse_arguments
        arguments = test_graph_core_cli.CoreCommandTests().arguments()
        self.assertFalse(parse_arguments(arguments).recover_restoring_sqrt_loops)
        self.assertTrue(parse_arguments(arguments + ['--recover-restoring-sqrt-loops']).recover_restoring_sqrt_loops)

    def test_pointwise_reuse_is_selected_before_local_and_sequential_pooling(self):
        flag = '--llm2fpga-coalesce-pointwise-pingpong'
        self.assertFalse(any(flag in s[2] for s in integer_profile()))
        stages = integer_profile(entrypoint='main', coalesce_pointwise_pingpong=True)
        scratch = next(s[2] for s in stages if s[0] == 'scratch')
        self.assertLess(scratch.index(flag), scratch.index('--llm2fpga-reuse-buffers'))
        self.assertLess(scratch.index(flag), scratch.index('--llm2fpga-pool-sequential-scratch=entrypoint=main'))
        with self.assertRaises(ValueError):
            integer_profile(coalesce_pointwise_pingpong=1)
        with tempfile.TemporaryDirectory() as temporary:
            base = test_graph_build_profile.BuildProfileTests().profile(Path(temporary))
            selected = replace(base, coalesce_pointwise_pingpong=True)
            self.assertNotEqual(base.identity_sha256, selected.identity_sha256)
            self.assertTrue(selected.document()['optimization_policy']['coalesce_pointwise_pingpong'])
            self.assertIn(flag, next(s['arguments'] for s in selected.document()['pass_sequence'] if s['name'] == 'scratch'))
        from compiler.compile_core import parse_arguments
        args = test_graph_core_cli.CoreCommandTests().arguments()
        self.assertFalse(parse_arguments(args).coalesce_pointwise_pingpong)
        self.assertTrue(parse_arguments(args + ['--coalesce-pointwise-pingpong']).coalesce_pointwise_pingpong)

    def test_transport_profile_reader_retains_both_exact_selections(self):
        import json
        from scripts.pipeline.export_graph_ddr_transport import read_profile
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = test_graph_build_profile.BuildProfileTests().profile(root)
            selected = replace(base, narrow_immutable=False, offchip_immutable=True,
                               recover_restoring_sqrt_loops=True, coalesce_pointwise_pingpong=True)
            core = root/'core'
            core.mkdir()
            (core/'profile.json').write_text(json.dumps(selected.document()))
            loaded = read_profile(core, selected.numerical_contract, selected.calyx_library)
            self.assertEqual(loaded.identity_sha256, selected.identity_sha256)
