#!/usr/bin/env python3
"""Reconcile the survey census, disjoint search ranges, exports, and evidence."""

import csv
from collections import Counter
import datetime as dt
import json
from pathlib import Path
import re


def main():
    base = Path(__file__).resolve().parent
    coverage = json.loads((base / 'coverage.json').read_text())
    analysis = json.loads((base / 'analysis.json').read_text())
    ids = json.loads((base / 'census-ids.json').read_text())
    id_set = set(ids)
    assert len(ids) == len(set(ids)) == coverage['census_unique_count']
    leaves = coverage['leaves']
    assert all(0 <= leaf['unique_count'] <= leaf['expected_count'] < 1000 for leaf in leaves)
    gaps = [leaf for leaf in leaves if leaf['unique_count'] != leaf['expected_count']]
    documented_queries = {entry.get('query') for entry in coverage.get('errors', [])}
    assert all(leaf['query'] in documented_queries for leaf in gaps), 'Undocumented count discrepancy'
    census_leaves = [leaf for leaf in leaves if leaf['base_query'] == 'language:lean']
    assert sum(leaf['unique_count'] for leaf in census_leaves) == len(ids)
    ranges = []
    for leaf in census_leaves:
        match = re.search(r' created:(\S+)\.\.(\S+)$', leaf['query'])
        assert match, leaf['query']
        ranges.append(tuple(int(dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp())
                            for value in match.groups()))
    ranges.sort()
    start = int(dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc).timestamp())
    end = int(dt.datetime(2026, 11, 1, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp())
    assert ranges[0][0] == start and ranges[-1][1] == end
    assert all(left[1] + 1 == right[0] for left, right in zip(ranges, ranges[1:])), 'Range gap/overlap'
    exports = {}
    for name, expected in analysis['export_counts'].items():
        with (base / name).open(newline='') as stream:
            rows = list(csv.DictReader(stream))
        assert len(rows) == expected, (name, len(rows), expected)
        assert len({row['full_name'] for row in rows}) == len(rows), name
        exports[name] = rows
    all_rows = exports['all-repositories.csv']
    assert {str(row['id']) for row in all_rows} == set(ids)
    assert all(row['fork'] == 'false' for row in all_rows)
    active = exports['top100-active.csv']
    assert len(active) == 100
    assert all(row['archived'] == 'false' and row['pushed_at'] >= '2026-09-01T00:00:00Z'
               for row in active)
    assert all(int(left['stars']) >= int(right['stars']) for left, right in zip(active, active[1:]))
    original = json.loads((base / 'top100-original-search.json').read_text())
    assert [row['full_name'] for row in exports['top100-all.csv']] == [row['full_name'] for row in original['items']]
    indexed_path = base / 'active-validation-query.json'
    if indexed_path.exists():
        indexed = json.loads(indexed_path.read_text())
        assert [row['full_name'] for row in exports['top100-github-query.csv']] == [row['full_name'] for row in indexed['items']]
    assert all(row['archived'] == 'false' and row['pushed_at'] >= '2026-09-01T00:00:00Z'
               for row in exports['active-keyword-matches.csv'])
    raw_hits = json.loads((base / 'keyword-hits.json').read_text())
    candidates = {row['full_name']: row for row in exports['candidates.csv']}
    assert set(raw_hits).issubset(candidates)
    requested = set(coverage['requested_keywords'])
    expected_requested = {name for name, terms in raw_hits.items() if set(terms) & requested}
    assert expected_requested.issubset({row['full_name'] for row in exports['keyword-matches.csv']})
    source = json.loads((base / 'source-scan.json').read_text())
    assert all('Verilog' not in row.get('file_keyword_matches', []) for row in source.values())
    report = (base / 'report.html').read_text()
    assert '__DATA__' not in report
    embedded = re.search(r'<script type="application/json" id="survey-data">(.*?)</script>', report, re.S)
    assert embedded
    payload = json.loads(embedded.group(1))
    assert len(payload['rows']) == analysis['candidate_count']
    visibility = {}
    cache = Path('/tmp/llm2fpga-lean-survey-20261101/cache')
    if cache.exists():
        for path in cache.glob('*.json'):
            cached = json.loads(path.read_text())
            if not cached.get('endpoint', '').startswith('search/repositories?'):
                continue
            for repo in cached.get('data', {}).get('items', []):
                if str(repo['id']) in id_set:
                    visibility[str(repo['id'])] = repo.get('visibility',
                        'private' if repo.get('private') else 'public')
        assert set(visibility) == set(ids), 'Visibility metadata missing for census IDs'
    result = {'status': 'passed_with_documented_search_gaps' if gaps else 'passed',
              'census_unique': len(ids),
              'disjoint_census_partitions': len(ranges), 'completed_search_leaves': len(leaves),
              'census_initial_count_difference': len(ids) - coverage['initial_search_total'],
              'documented_count_discrepancy_leaves': len(gaps),
              'count_only_census_matches': analysis['count_only_census_matches'],
              'exports': {name: len(rows) for name, rows in exports.items()},
              'source_filename_inventories': len(source),
              'filename_inventory_errors': sum(bool(row.get('errors')) for row in source.values()),
              'truncated_filename_inventories': sum(bool(row.get('tree_truncated')) for row in source.values()),
              'repository_visibility_counts': dict(Counter(visibility.values())),
              'source_code_inspected_candidates': analysis['source_code_inspected_candidate_count'],
              'curated_source_assessments': analysis['source_reviewed_candidate_count'],
              'github_indexed_recent_query_total': analysis['github_indexed_recent_query_total'],
              'github_indexed_top100_agrees_with_metadata': analysis['github_indexed_top100_agrees_with_metadata'],
              'note': 'Reconciles downloaded/indexed search results and generated artifacts; not proof of GitHub index completeness or candidate implementation correctness.'}
    (base / 'verification.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
