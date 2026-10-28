"""Review each native source patch beside its authenticated original source."""
from pathlib import Path
import tempfile
import unittest
from compiler import review


class UpstreamPatchInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.patches = self.root / 'nix/patches'
        self.patches.mkdir(parents=True)
        self.sources = {}
        for name in ('calyx', 'circt', 'torch_mlir', 'vast', 'iverilog'):
            path = self.root / name
            path.mkdir()
            self.sources[name] = path
        self.original = self.sources['calyx'] / 'calyx/opt/src/passes/compile_invoke.rs'
        self.original.parent.mkdir(parents=True)
        self.original.write_text('original native pass\n')
        self.patch = self.patches / 'graph-invoke.patch'
        self.patch.write_text('--- a/calyx/opt/src/passes/compile_invoke.rs\n'
            '+++ b/calyx/opt/src/passes/compile_invoke.rs\n'
            '@@ -1 +1 @@\n-original native pass\n+changed native pass\n')

    def inventory(self):
        function = getattr(review, 'upstream_patch_inventory', None)
        self.assertIsNotNone(function, 'upstream patch source inventory is missing')
        return function(self.root, self.sources)

    def test_patch_and_original_source_are_both_hashed(self):
        result = self.inventory()
        self.assertIn(str(self.patch), result['dependency_hashes'])
        self.assertIn(str(self.original), result['dependency_hashes'])
        self.assertEqual(result['patches'][0]['original_sources'], [str(self.original)])
        before = result['identity_sha256']
        self.original.write_text('changed upstream input\n')
        self.assertNotEqual(before, self.inventory()['identity_sha256'])

    def test_missing_original_source_is_rejected(self):
        self.original.unlink()
        with self.assertRaisesRegex(ValueError, 'source'):
            self.inventory()

    def test_mux_grouping_patch_uses_pinned_native_reduction_source(self):
        source = self.root / 'yosys-source'
        original = source / 'passes/opt/opt_reduce.cc'
        original.parent.mkdir(parents=True)
        original.write_text('native mux reduction\n')
        self.sources['yosys'] = source
        patch = self.patches / 'graph-yosys-keyed-mux-grouping.patch'
        patch.write_text('--- a/passes/opt/opt_reduce.cc\n'
                         '+++ b/passes/opt/opt_reduce.cc\n'
                         '@@ -1 +1 @@\n-native mux reduction\n+keyed grouping\n')
        result = self.inventory()
        record = next(row for row in result['patches'] if row['patch'] == str(patch))
        self.assertEqual(record['original_sources'], [str(original)])
        self.assertIn(str(original), result['dependency_hashes'])
        original.unlink()
        with self.assertRaisesRegex(ValueError, 'source'):
            self.inventory()

    def test_constant_evaluator_patch_uses_pinned_native_header(self):
        source = self.root / 'yosys-source'
        original = source / 'kernel/consteval.h'
        original.parent.mkdir(parents=True)
        original.write_text('native evaluator header\n')
        self.sources['yosys'] = source
        patch = self.patches / 'graph-yosys-consteval-lazy-mux.patch'
        patch.write_text('--- a/kernel/consteval.h\n+++ b/kernel/consteval.h\n'
                         '@@ -1 +1 @@\n-native evaluator header\n+lazy evaluator\n')
        result = self.inventory()
        record = next(row for row in result['patches'] if row['patch'] == str(patch))
        self.assertEqual(record['original_sources'], [str(original)])
        self.assertIn(str(original), result['dependency_hashes'])
        original.unlink()
        with self.assertRaisesRegex(ValueError, 'source'):
            self.inventory()

    def test_fsm_optimizer_patch_uses_pinned_native_source(self):
        source = self.root / 'yosys-source'
        original = source / 'passes/fsm/fsm_opt.cc'
        original.parent.mkdir(parents=True)
        original.write_text('native evaluator header\n')
        self.sources['yosys'] = source
        patch = self.patches / 'graph-yosys-fsm-cached-bits.patch'
        patch.write_text('--- a/passes/fsm/fsm_opt.cc\n+++ b/passes/fsm/fsm_opt.cc\n'
                         '@@ -1 +1 @@\n-native evaluator header\n+lazy evaluator\n')
        result = self.inventory()
        record = next(row for row in result['patches'] if row['patch'] == str(patch))
        self.assertEqual(record['original_sources'], [str(original)])
        self.assertIn(str(original), result['dependency_hashes'])
        original.unlink()
        with self.assertRaisesRegex(ValueError, 'source'):
            self.inventory()

    def test_signed_divider_metadata_patch_uses_pinned_primitive_source(self):
        original = self.sources['calyx'] / 'primitives/binary_operators.futil'
        original.parent.mkdir(parents=True)
        original.write_text('original primitive contract\n')
        patch = self.patches / 'graph-calyx-signed-divider-sharing.patch'
        patch.write_text('--- a/primitives/binary_operators.futil\n'
                         '+++ b/primitives/binary_operators.futil\n'
                         '@@ -1 +1 @@\n-original primitive contract\n+sharing contract\n')
        result = self.inventory()
        record = next(row for row in result['patches'] if row['patch'] == str(patch))
        self.assertEqual(record['original_sources'], [str(original)])
        self.assertIn(str(original), result['dependency_hashes'])
        original.unlink()
        with self.assertRaisesRegex(ValueError, 'source'):
            self.inventory()

    def test_yosys_mapping_patch_is_bound_to_the_original_runtime_source(self):
        source = self.root / 'yosys-data'
        original = source / 'xilinx/brams_xc6v_map.v'
        original.parent.mkdir(parents=True)
        original.write_text('original native mapping template\n')
        self.sources['yosys'] = source
        patch = self.patches / 'graph-yosys-sdp72-upper-parity.patch'
        patch.write_text('--- a/techlibs/xilinx/brams_xc6v_map.v\n'
                         '+++ b/techlibs/xilinx/brams_xc6v_map.v\n'
                         '@@ -1 +1 @@\n-original native mapping template\n+corrected native mapping template\n')
        result = self.inventory()
        record = next(row for row in result['patches'] if row['patch'] == str(patch))
        self.assertEqual(record['original_sources'], [str(original)])
        self.assertIn(str(original), result['dependency_hashes'])

    def test_yosys_kernel_patch_uses_pinned_compiler_source(self):
        source = self.root / 'yosys-source'
        original = source / 'kernel/gzip.cc'
        original.parent.mkdir(parents=True)
        original.write_text('native output writer\n')
        self.sources['yosys'] = source
        patch = self.patches / 'graph-yosys-bounded-gzip.patch'
        patch.write_text('--- a/kernel/gzip.cc\n+++ b/kernel/gzip.cc\n'
                         '@@ -1 +1 @@\n-native output writer\n+bounded writer\n')
        result = self.inventory()
        self.assertIn(str(original), result['dependency_hashes'])

    def test_yosys_deminout_patch_uses_pinned_native_pass_source(self):
        source = self.root / 'yosys-source'
        original = source / 'passes/techmap/deminout.cc'
        original.parent.mkdir(parents=True)
        original.write_text('native inout demotion pass\n')
        self.sources['yosys'] = source
        patch = self.patches / 'graph-yosys-deminout-no-inout.patch'
        patch.write_text('--- a/passes/techmap/deminout.cc\n'
                         '+++ b/passes/techmap/deminout.cc\n'
                         '@@ -1 +1 @@\n-native inout demotion pass\n+fast path\n')
        result = self.inventory()
        record = next(row for row in result['patches'] if row['patch'] == str(patch))
        self.assertEqual(record['original_sources'], [str(original)])
        self.assertIn(str(original), result['dependency_hashes'])

    def test_yosys_proc_prune_patch_uses_pinned_native_pass_source(self):
        source = self.root / 'yosys-source'
        original = source / 'passes/proc/proc_prune.cc'
        original.parent.mkdir(parents=True)
        original.write_text('native process pruning pass\n')
        self.sources['yosys'] = source
        patch = self.patches / 'graph-yosys-proc-prune-empty.patch'
        patch.write_text('--- a/passes/proc/proc_prune.cc\n'
                         '+++ b/passes/proc/proc_prune.cc\n'
                         '@@ -1 +1 @@\n-native process pruning pass\n+fast path\n')
        result = self.inventory()
        record = next(row for row in result['patches'] if row['patch'] == str(patch))
        self.assertEqual(record['original_sources'], [str(original)])
        self.assertIn(str(original), result['dependency_hashes'])

    def test_flattening_patch_resolves_the_pinned_circt_transform(self):
        original = self.sources['circt'] / 'lib/Transforms/FlattenMemRefs.cpp'
        original.parent.mkdir(parents=True)
        original.write_text('original row-major transform\n')
        patch = self.patches / 'graph-memref-row-major-flatten.patch'
        patch.write_text('--- a/FlattenMemRefs.cpp\n+++ b/FlattenMemRefs.cpp\n'
                         '@@ -1 +1 @@\n-original row-major transform\n+corrected transform\n')
        result = self.inventory()
        record = next(row for row in result['patches'] if row['patch'] == str(patch))
        self.assertEqual(record['original_sources'], [str(original)])
        self.assertIn(str(original), result['dependency_hashes'])

    def test_unknown_or_escaping_patch_target_is_rejected(self):
        self.patch.write_text('--- a/../outside.rs\n+++ b/../outside.rs\n')
        with self.assertRaisesRegex(ValueError, 'target'):
            self.inventory()
