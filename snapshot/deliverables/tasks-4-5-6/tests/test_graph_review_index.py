"""Review preparation authenticates dependencies without granting acceptance."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from compiler import review
from compiler.build_core import CoreReceipt
from compiler.stages import StageSpec, _identity, digest, run_stage


class ReviewIndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        source = self.root / 'input.mlir'
        source.write_text('receipt fixture; no model semantics')
        tool = Path(shutil.which('cp')).resolve()
        self.stage = self.root / 'sv'
        spec = StageSpec('sv', (str(tool), str(source), 'kernel.sv'),
            {str(source): digest(source)}, {str(tool): digest(tool)},
            {'scope': 'synthetic review gate'}, ('kernel.sv',))
        run_stage(spec, self.stage)
        self.dependencies = {str(path): digest(path) for path in
            (source, tool, self.stage / 'spec.json', self.stage / 'success.json',
             self.stage / 'kernel.sv')}
        self.profile_sha256 = '0' * 64

    def receipt(self):
        body = dict(schema_version=1, profile_sha256=self.profile_sha256,
            materialized_inputs_sha256='1' * 64, output=str(self.stage / 'kernel.sv'),
            graphs=tuple({'context': n} for n in range(4, 20)),
            dependency_hashes=self.dependencies)
        return CoreReceipt(**body, identity_sha256=_identity(body))

    def build(self):
        function = getattr(review, 'build_review_index', None)
        self.assertIsNotNone(function, 'composed review index is missing')
        return function(self.receipt(), ())

    def test_core_and_native_dependencies_are_all_indexed_without_approval(self):
        result = self.build()
        for name in self.dependencies:
            self.assertIn(name, result['dependency_hashes'])
        source_root = Path(__file__).resolve().parents[1]
        for name in review.compiler_source_inventory(source_root)['dependency_hashes']:
            self.assertIn(name, result['dependency_hashes'])
        self.assertEqual([c['chunk_id'] for c in result['chunks']],
                         [f'{n:02d}' for n in range(1, 12)])
        self.assertFalse(result['ready_for_manual_review'])
        for chunk in result['chunks']:
            self.assertIsNone(chunk['user_decision'])
            self.assertIsNone(chunk['approved_commit'])
            self.assertEqual(chunk['state'], 'preparation-open')

    def test_native_compiler_build_regression_source_is_readable(self):
        result = self.build()
        root = Path(__file__).resolve().parents[1]
        path = root / 'tests/yosys_gzip_large_write.cpp'
        self.assertEqual(result['chunks'][7]['files'].get(str(path)), digest(path))
        self.assertFalse(result['ready_for_manual_review'])

    def test_native_yosys_pass_policy_is_in_synthesis_review_chunk(self):
        result = self.build()
        root = Path(__file__).resolve().parents[1]
        path = root / 'compiler/yosys_pipeline.py'
        self.assertEqual(result['chunks'][9]['files'].get(str(path)), digest(path))
        self.assertFalse(result['ready_for_manual_review'])

    def test_frozen_value_certificate_is_in_memory_review_chunk(self):
        result = self.build()
        root = Path(__file__).resolve().parents[1]
        path = root / 'compiler/frozen_ranges.py'
        self.assertEqual(result['chunks'][5]['files'].get(str(path)), digest(path))
        self.assertFalse(result['ready_for_manual_review'])

    def test_read_value_bounds_planner_is_in_memory_review_chunk(self):
        result = self.build()
        root = Path(__file__).resolve().parents[1]
        path = root / 'compiler/read_value_bounds.py'
        self.assertEqual(result['chunks'][5]['files'].get(str(path)), digest(path))
        self.assertFalse(result['ready_for_manual_review'])

    def test_generation_verification_sources_are_in_numerical_review_chunk(self):
        result = self.build()
        root = Path(__file__).resolve().parents[1]
        chunk = result['chunks'][10]
        for relative in ('scripts/comparison/graph_rtl_runtime.py',
                         'scripts/comparison/verify_graph_generation_rtl.py',
                         'scripts/comparison/verify_closure_reference.py',
                         'scripts/comparison/graph_generation_progress_main.cpp'):
            path = str(root / relative)
            with self.subTest(source=relative):
                self.assertIn(path, chunk['files'])
                self.assertEqual(chunk['files'][path], digest(Path(path)))
        self.assertFalse(result['ready_for_manual_review'])

    def test_production_synthesis_driver_is_in_build_review_chunk(self):
        result = self.build()
        root = Path(__file__).resolve().parents[1]
        for name in ('export_graph_core_synthesis.py', 'map_graph_core_xc7.py',
                     'export_graph_ddr_transport.py'):
            path = root / 'scripts/pipeline' / name
            self.assertEqual(result['chunks'][6]['files'].get(str(path)), digest(path),
                             'production synthesis driver missing from chunk 07')
        self.assertFalse(result['ready_for_manual_review'])

    def test_board_input_and_controller_sources_are_in_board_review_chunk(self):
        result = self.build()
        root = Path(__file__).resolve().parents[1]
        for relative in ('compiler/board_pins.py', 'compiler/litedram_export.py'):
            path = root / relative
            self.assertEqual(result['chunks'][8]['files'].get(str(path)), digest(path),
                             'board compiler input source missing from chunk 09')
        self.assertFalse(result['ready_for_manual_review'])

    def test_original_task2_input_and_provenance_are_in_capture_review_chunk(self):
        result = self.build()
        root = Path(__file__).resolve().parents[1]
        for relative in ('model_inputs/task2/matmul.py', 'model_inputs/task2/provenance.json'):
            path = root / relative
            self.assertEqual(result['chunks'][0]['files'].get(str(path)), digest(path))
        self.assertFalse(result['ready_for_manual_review'])

    def frozen_profile(self, changed=False):
        root = Path(__file__).resolve().parents[1]
        frozen = self.root / 'immutable-source'
        identities = {}
        for relative in ('compiler/frontend/dependencies.json',
                         'compiler/frontend/capture.py'):
            path = frozen / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((root / relative).read_bytes())
            if changed and relative.endswith('capture.py'):
                path.write_text('different compiler implementation')
            identities[str(path)] = digest(path)
        profile = self.root / 'profile.json'
        document = {'dependency_hashes': identities}
        profile.write_text(json.dumps(document))
        self.profile_sha256 = _identity(document)
        self.dependencies.update(identities)
        self.dependencies[str(profile)] = digest(profile)
        return frozen, root

    def test_immutable_compiler_sources_are_bound_to_local_reading_chunks(self):
        frozen, root = self.frozen_profile()
        result = self.build()
        original = str(frozen / 'compiler/frontend/capture.py')
        local = str(root / 'compiler/frontend/capture.py')
        self.assertEqual(result['compiled_source_origins'][local], original)
        self.assertEqual(result['chunks'][0]['files'][local], digest(Path(local)))
        self.assertFalse(result['ready_for_manual_review'])

    def test_changed_compiled_source_cannot_be_replaced_by_local_candidate(self):
        self.frozen_profile(changed=True)
        with self.assertRaisesRegex(ValueError, 'compiled source'):
            self.build()

    def test_missing_declared_dependency_is_rejected(self):
        (self.root / 'input.mlir').unlink()
        with self.assertRaisesRegex(ValueError, 'dependency'):
            self.build()

    def test_native_passes_are_read_with_their_semantic_contracts(self):
        result = self.build()
        owners = {Path(name).name: chunk['chunk_id']
                  for chunk in result['chunks'] for name in chunk['files']}
        for source, expected_chunk in (
                ('TypedTensor.h', '03'),
                ('BufferReuse.cpp', '05'),
                ('FunctionSharing.cpp', '05'),
                ('ExclusiveCombInputs.cpp', '05'),
                ('IdenticalComb.cpp', '05'),
                ('TransactionalDividers.cpp', '05'),
                ('RegisterBoundedComb.cpp', '05'),
                ('StaticLoopCounters.cpp', '05'),
                ('PrivateStorage.cpp', '04'),
                ('PointwiseFusion.cpp', '05'),
                ('MemoryBindings.cpp', '06'),
                ('MemoryRetirement.cpp', '06'),
                ('RuntimeInterface.cpp', '09')):
            with self.subTest(source=source):
                self.assertEqual(owners[source], expected_chunk)
        reading_order = [name for chunk in result['chunks']
                         for name in chunk['reading_order']]
        self.assertEqual(len(reading_order), len(set(reading_order)))
        self.assertFalse(result['ready_for_manual_review'])

    def test_omitted_stage_dependency_is_rejected(self):
        del self.dependencies[str(self.root / 'input.mlir')]
        with self.assertRaisesRegex(ValueError, 'stage dependency'):
            self.build()

    def test_failed_stage_is_rejected_even_with_updated_file_hash(self):
        path = self.stage / 'success.json'
        data = json.loads(path.read_text())
        data['success'] = False
        path.write_text(json.dumps(data))
        self.dependencies[str(path)] = digest(path)
        with self.assertRaisesRegex(ValueError, 'stage'):
            self.build()
