"""Memory selections must reach capture buffers and the authenticated controller."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from compiler.pipeline import integer_profile
import test_graph_build_profile as profiles
import test_graph_core_cli as commands


class MemoryReductionPolicyTests(unittest.TestCase):
    def test_fusion_precedes_bufferization_and_unit_folding(self):
        stages=integer_profile(fuse_gemv_epilogues=True)
        args=next(s[2] for s in stages if s[0]=='buffers')
        fuse=args.index('--llm2fpga-fuse-gemv-epilogues')
        self.assertLess(args.index('--llm2fpga-fold-producer-slices'),fuse)
        self.assertLess(fuse,args.index('--linalg-fold-unit-extent-dims'))
        self.assertLess(args.index('--linalg-fold-unit-extent-dims'),args.index('--llm2fpga-protect-functional-inputs'))
        self.assertFalse(any('--llm2fpga-fuse-gemv-epilogues' in s[2] for s in integer_profile()))

    def test_capacity_runs_after_existing_interprocedural_pool(self):
        stages=integer_profile(entrypoint='main',pool_private_capacity=True)
        args=next(s[2] for s in stages if s[0]=='scratch')
        self.assertLess(args.index('--llm2fpga-pool-sequential-scratch=entrypoint=main'),
                        args.index('--llm2fpga-pool-private-capacity'))
        self.assertIn('--llm2fpga-pool-private-capacity',next(s[2] for s in integer_profile(pool_private_capacity=True) if s[0]=='scratch'))

    def test_invalid_flags_do_not_access_inputs(self):
        for name in ('fuse_gemv_epilogues','pool_private_capacity'):
            for value in (1,None,'yes'):
                with self.subTest(name=name,value=value),self.assertRaises(ValueError):
                    integer_profile(**{name:value})

    def test_profile_identity_cli_and_reader_retain_both_flags(self):
        from compiler.compile_core import parse_arguments
        from scripts.pipeline.export_graph_ddr_transport import read_profile
        args=commands.CoreCommandTests().arguments()
        self.assertFalse(parse_arguments(args).fuse_gemv_epilogues)
        self.assertFalse(parse_arguments(args).pool_private_capacity)
        selected_args=parse_arguments(args+['--fuse-gemv-epilogues','--pool-private-capacity'])
        self.assertTrue(selected_args.fuse_gemv_epilogues)
        self.assertTrue(selected_args.pool_private_capacity)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);base=profiles.BuildProfileTests().profile(root)
            selected=replace(base,fuse_gemv_epilogues=True,pool_private_capacity=True)
            self.assertNotEqual(base.identity_sha256,selected.identity_sha256)
            self.assertTrue(selected.document()['optimization_policy']['fuse_gemv_epilogues'])
            self.assertTrue(selected.document()['optimization_policy']['pool_private_capacity'])
            core=root/'core';core.mkdir();(core/'profile.json').write_text(json.dumps(selected.document()))
            loaded=read_profile(core,selected.numerical_contract,selected.calyx_library)
            self.assertEqual(loaded.identity_sha256,selected.identity_sha256)
            for name in ('fuse_gemv_epilogues','pool_private_capacity'):
                with self.assertRaises(ValueError):replace(base,**{name:1})
