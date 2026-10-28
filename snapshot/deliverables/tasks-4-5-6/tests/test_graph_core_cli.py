"""The compilation command exposes model inputs, never answer fixtures."""
import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

class CoreCommandTests(unittest.TestCase):
    def command(self):
        self.assertIsNotNone(importlib.util.find_spec('compiler.compile_core'),
                             'compilation-only entry point missing')
        from compiler.compile_core import parse_arguments
        return parse_arguments

    def arguments(self):
        args = ['--snapshot', '/snapshot', '--package', '/package',
                '--numerical-contract', '/contract', '--output', '/output',
                '--calyx-library', '/library']
        for name in ('torch', 'circt', 'memory', 'translate', 'calyx'):
            args += ['--' + name + '-tool', '/tools/' + name]
        return args

    def rejects(self, extra, diagnostic):
        parser = self.command()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), self.assertRaises(SystemExit) as error:
            parser(self.arguments() + extra)
        self.assertEqual(error.exception.code, 2)
        self.assertIn(diagnostic, stderr.getvalue())

    def test_expected_answers_are_not_compiler_arguments(self):
        self.rejects(['--expected-results', '/answers'], 'unrecognized arguments')

    def test_context_coverage_cannot_be_reduced_from_command(self):
        self.rejects(['--contexts', '4'], 'unrecognized arguments')

    def test_timeout_must_be_positive_and_finite(self):
        for value in ('0', '-1', 'nan', 'inf'):
            with self.subTest(value=value):
                self.rejects(['--timeout-seconds', value], 'positive finite')

    def test_rejected_input_invalidates_previous_success_receipts(self):
        from compiler.compile_core import main
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            output = root / 'output'
            stage = output / 'contexts/context-04/buffers'
            stage.mkdir(parents=True)
            receipts = [output / 'core.json', stage / 'success.json']
            for path in receipts:
                path.write_text('{"success": true}')
            diagnostic = stage / 'stderr.log'
            diagnostic.write_text('previous compiler diagnostic')
            arguments = self.arguments()
            for flag in ('snapshot', 'package', 'numerical-contract'):
                arguments[arguments.index('--' + flag) + 1] = str(root / flag)
            arguments[arguments.index('--output') + 1] = str(output)
            with self.assertRaises(FileNotFoundError):
                main(arguments)
            for path in receipts:
                self.assertFalse(path.exists(), 'failed invocation retained success: ' + str(path))
            self.assertEqual(diagnostic.read_text(), 'previous compiler diagnostic')
