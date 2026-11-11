#!/usr/bin/env python3
import argparse
from collections import Counter
import io
import json
from pathlib import Path
import subprocess
import tarfile

parser = argparse.ArgumentParser(
    description='Count reviewable text, including blanks and comments.'
)
parser.add_argument('--ref', help='Count a Git revision instead of the working tree.')
parser.add_argument('paths', nargs='*', help='Optional repository-relative files or directories.')
args = parser.parse_args()
selectors = [str(Path(path)) for path in args.paths]
root = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip())


def git(*arguments):
    return subprocess.check_output(['git', '-C', str(root), *arguments])


def files():
    if args.ref:
        archive = git('archive', args.ref)
        with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
            for entry in tree:
                if entry.isfile():
                    yield entry.name, tree.extractfile(entry).read()
    else:
        names = git('ls-files', '-z', '--cached', '--others', '--exclude-standard')
        for name in sorted(set(names.decode().split('\0'))):
            path = root / name
            if path.is_file() and not path.is_symlink():
                yield name, path.read_bytes()


source_extensions = {
    '.py', '.sh', '.nix', '.c', '.cc', '.cpp', '.h', '.hpp', '.rs', '.v', '.sv',
    '.futil', '.il', '.mlir', '.tcl', '.xdc', '.sdc', '.cmake',
}
languages, categories = Counter(), Counter()
long_files = {}
count = total = maximum = 0
for name, data in files():
    if selectors and not any(
        path == '.' or name == path or name.startswith(path + '/')
        for path in selectors
    ):
        continue
    if b'\0' in data:
        continue
    lines = data.splitlines()
    widths = [len(line.decode(errors='replace').expandtabs(8)) for line in lines]
    path = Path(name)
    if path.suffix in source_extensions or path.name in ('CMakeLists.txt', 'Makefile'):
        category = 'source'
    elif path.suffix == '.patch':
        category = 'patches'
    elif path.suffix in ('.json', '.yaml', '.yml', '.toml', '.lock'):
        category = 'data_config_lock'
    else:
        category = 'other'
    categories[category] += len(lines)
    languages[path.suffix or path.name] += len(lines)
    count += 1
    total += len(lines)
    maximum = max(maximum, max(widths, default=0))
    if any(width > 100 for width in widths):
        long_files[name] = {
            'lines': sum(width > 100 for width in widths), 'max_columns': max(widths)
        }
print(json.dumps({
    'total_lines': total, 'files': count,
    'source_and_patch_lines': categories['source'] + categories['patches'],
    'category_lines': dict(sorted(categories.items())),
    'language_lines': dict(sorted(languages.items())),
    'line_width': {
        'limit': 100, 'over_limit': sum(value['lines'] for value in long_files.values()),
        'max_columns': maximum, 'files': dict(sorted(long_files.items())),
    },
}, indent=2))
