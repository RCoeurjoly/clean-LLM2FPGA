#!/usr/bin/env python3
"""Collect file-tree/language evidence for top-100 and source-triaged leads.

This is a filename inventory, not a behavioral or proof audit. Uses read-only gh
REST calls and shares the census cache. Missing/truncated trees are explicit.
"""

import argparse
import base64
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
import json
from pathlib import Path
import subprocess
import time
import urllib.parse

from collect import Collector, save_json

SUFFIXES = {'.lean', '.py', '.sv', '.v', '.vhd', '.vhdl', '.mlir'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path(__file__).parent)
    parser.add_argument('--cache', type=Path, default=Path('/tmp/llm2fpga-lean-survey-20261101/cache'))
    parser.add_argument('--strong-indexed', action='store_true')
    args = parser.parse_args()
    api = Collector(args.out, args.cache)
    top = json.loads((args.out / 'top100-original-search.json').read_text())['items']
    metadata = {row['full_name']: row for row in top}
    names = set(metadata)
    for path in args.out.glob('curated-*.json'):
        rows = json.loads(path.read_text())
        if isinstance(rows, dict):
            rows = rows.get('repositories', rows.get('entries', rows.get('candidates', [])))
        names.update(row['full_name'] for row in rows)
    if args.strong_indexed:
        hits = json.loads((args.out / 'keyword-hits.json').read_text())
        names.update(name for name, terms in hits.items()
                     if set(terms) & {'Verilog', 'SystemVerilog', 'VHDL', 'yosys', 'RTL', 'MLIR', 'PyTorch', 'CIRCT', 'Calyx', 'FPGA'})
    for line in (args.out / 'repositories.jsonl').read_text().splitlines():
        row = json.loads(line)
        if row['full_name'] in names:
            metadata[row['full_name']] = row
    output = {}
    existing = args.out / 'source-scan.json'
    if existing.exists():
        output = json.loads(existing.read_text())

    def inspect(name):
        result = {'full_name': name, 'inspection_kind': 'filename and GitHub language inventory; not source correctness audit',
                  'errors': [], 'file_keyword_matches': []}
        repo = metadata.get(name)
        if not repo:
            try:
                repo = api.api('repos/' + name)
                metadata[name] = repo
            except Exception as error:
                result['errors'].append(str(error)[:400])
                return name, result
        result['metadata'] = {key: repo.get(key) for key in
                              ('id', 'full_name', 'html_url', 'description', 'language', 'stargazers_count',
                               'archived', 'fork', 'pushed_at', 'default_branch', 'created_at', 'updated_at')}
        try:
            result['languages_bytes'] = api.api('repos/' + name + '/languages')
        except Exception as error:
            result['errors'].append('languages: ' + str(error)[:300])
        try:
            branch = urllib.parse.quote(repo.get('default_branch') or 'HEAD', safe='')
            tree = api.api('repos/' + name + '/git/trees/' + branch + '?recursive=1')
            paths = [row['path'] for row in tree.get('tree', []) if row.get('type') == 'blob']
            result['tree_sha'] = tree.get('sha')
            result['tree_truncated'] = tree.get('truncated', False)
            result['blob_count_returned'] = len(paths)
            counts = Counter(Path(path).suffix.lower() for path in paths)
            result['extension_counts'] = {suffix: counts[suffix] for suffix in sorted(SUFFIXES)}
            result['sample_paths'] = {suffix: [path for path in paths if Path(path).suffix.lower() == suffix][:8]
                                      for suffix in sorted(SUFFIXES) if counts[suffix]}
            if counts['.py']:
                result['file_keyword_matches'].append('Python')
            if counts['.sv']:
                result['file_keyword_matches'].append('SystemVerilog')
            if counts['.vhd'] or counts['.vhdl']:
                result['file_keyword_matches'].append('VHDL')
            if counts['.mlir']:
                result['file_keyword_matches'].append('MLIR')
            # .v is ambiguous: Coq and Verilog share it. Preserve evidence without
            # automatically classifying a .v filename as Verilog.
            result['v_extension_ambiguous'] = bool(counts['.v'])
        except Exception as error:
            result['errors'].append('tree: ' + str(error)[:300])
        return name, result

    pending = sorted(name for name in names if name not in output)
    print(f'File inventories planned={len(names)} cached_summaries={len(output)} pending={len(pending)}', flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(inspect, name) for name in pending]
        for future in as_completed(jobs):
            name, row = future.result()
            output[name] = row
            save_json(existing, output)
            print(f'File inventory {len(output)}/{len(names)}: {name}', flush=True)
    with (args.out / 'source-file-matches.csv').open('w', newline='') as stream:
        fields = ['full_name', 'stars', 'pushed_at', 'archived', 'file_keyword_matches', 'tree_truncated',
                  'lean_files', 'python_files', 'sv_files', 'ambiguous_v_files', 'vhdl_files', 'mlir_files', 'sample_paths', 'errors']
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for name, row in sorted(output.items()):
            m, e = row.get('metadata', {}), row.get('extension_counts', {})
            writer.writerow({'full_name': name, 'stars': m.get('stargazers_count'), 'pushed_at': m.get('pushed_at'),
                             'archived': m.get('archived'), 'file_keyword_matches': ' | '.join(row['file_keyword_matches']),
                             'tree_truncated': row.get('tree_truncated'), 'lean_files': e.get('.lean'),
                             'python_files': e.get('.py'), 'sv_files': e.get('.sv'), 'ambiguous_v_files': e.get('.v'),
                             'vhdl_files': (e.get('.vhd', 0) + e.get('.vhdl', 0)), 'mlir_files': e.get('.mlir'),
                             'sample_paths': json.dumps(row.get('sample_paths', {})), 'errors': ' | '.join(row['errors'])})
    print('FILE INVENTORY COMPLETE', flush=True)


if __name__ == '__main__':
    main()
