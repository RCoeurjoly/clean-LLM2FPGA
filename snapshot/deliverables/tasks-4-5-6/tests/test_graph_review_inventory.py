"""Source review must include authored native compiler transformations."""
from pathlib import Path
import tempfile
import unittest
from compiler import review


class ReviewInventoryTests(unittest.TestCase):
    def inventory(self, root):
        function = getattr(review, 'compiler_source_inventory', None)
        self.assertIsNotNone(function, 'native compiler review inventory is missing')
        return function(root)

    def test_native_passes_helpers_build_recipes_and_patches_are_enumerated(self):
        root = Path(__file__).resolve().parents[1]
        inventory = self.inventory(root)
        names = inventory['dependency_hashes']
        for directory in ('compiler/passes', 'compiler/native'):
            for path in (root / directory).rglob('*'):
                if path.is_file():
                    self.assertIn(str(path), names)
        for path in (root / 'nix').glob('graph-*.nix'):
            self.assertIn(str(path), names)
        for path in (root / 'nix/patches').glob('graph-*.patch'):
            self.assertIn(str(path), names)
        self.assertIn(str(root / 'flake.lock'), names)

    def test_added_or_changed_native_source_changes_inventory_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('compiler/passes', 'compiler/native', 'nix/patches', 'tests'):
                (root / name).mkdir(parents=True)
            for name in ('compiler/passes/pass.cpp', 'compiler/native/helper.rs',
                         'nix/graph-tool.nix', 'nix/patches/graph-tool.patch',
                         'flake.nix', 'flake.lock',
                         'tests/yosys_gzip_large_write.cpp'):
                (root / name).write_text('initial source\n')
            before = self.inventory(root)
            (root / 'compiler/passes/new.cpp').write_text('new source\n')
            added = self.inventory(root)
            self.assertNotEqual(before['identity_sha256'], added['identity_sha256'])
            (root / 'nix/patches/graph-tool.patch').write_text('changed native pass\n')
            changed = self.inventory(root)
            self.assertNotEqual(added['identity_sha256'], changed['identity_sha256'])
            self.assertNotIn(str(root / 'tests'), changed['dependency_hashes'])
