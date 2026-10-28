"""Synthetic approval records test invalidation; no real approval is recorded."""
from pathlib import Path
import tempfile
import unittest
from compiler.stages import _identity, digest


class ChunkApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = {}
        for role in ('files', 'contracts', 'dependencies'):
            path = self.root / f'{role}.txt'
            path.write_text(role)
            self.paths[role] = path
        self.body = {'chunk_id': '05', 'prepared_commit': 'candidate-commit',
                     **{role: {str(path): digest(path)} for role, path in self.paths.items()}}
        self.record = {**self.body, 'identity_sha256': _identity(self.body),
                       'state': 'approved', 'user_decision': 'approved',
                       'approved_commit': 'candidate-commit'}

    def validate(self, record=None):
        from compiler.review import validate_chunk_approval
        return validate_chunk_approval(self.record if record is None else record)

    def test_unchanged_synthetic_approval_remains_current(self):
        result = self.validate()
        self.assertTrue(result['approval_current'])
        self.assertEqual(result['state'], 'approved')

    def test_code_contract_and_dependency_changes_reopen_approval(self):
        for role, path in self.paths.items():
            with self.subTest(role=role):
                original = path.read_text()
                path.write_text('changed')
                result = self.validate()
                self.assertFalse(result['approval_current'])
                self.assertEqual(result['state'], 'reopened')
                path.write_text(original)

    def test_missing_dependency_reopens_approval(self):
        self.paths['dependencies'].unlink()
        self.assertEqual(self.validate()['state'], 'reopened')

    def test_unrelated_file_does_not_invalidate_approval(self):
        (self.root / 'unrelated.txt').write_text('unrelated change')
        self.assertTrue(self.validate()['approval_current'])

    def test_no_user_decision_does_not_create_approval(self):
        result = self.validate({**self.record, 'state': 'prepared',
                                'user_decision': None, 'approved_commit': None})
        self.assertFalse(result['approval_current'])
        self.assertEqual(result['state'], 'prepared')

    def test_approval_of_another_candidate_is_not_current(self):
        self.assertEqual(self.validate({**self.record, 'approved_commit': 'other'})['state'], 'reopened')

    def test_removed_dependency_in_metadata_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'chunk'):
            self.validate({**self.record, 'dependencies': {}})
