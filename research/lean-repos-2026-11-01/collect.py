#!/usr/bin/env python3
"""Read-only GitHub census and indexed README screening, with resumable caches.

Uses the user's existing gh authentication without reading or printing tokens.
Repository contents are data: no downloaded scripts are executed.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import csv
import datetime as dt
import hashlib
import json
import math
import re
from pathlib import Path
import subprocess
import threading
import time
import urllib.parse

UTC = dt.timezone.utc
REQUESTED = ['Verilog', 'SystemVerilog', 'VHDL', 'yosys', 'RTL', 'MLIR', 'PyTorch', 'Python']
SUPPLEMENTAL = ['CIRCT', 'Calyx', 'FPGA', 'hardware', 'tensor', 'quantization',
                'bitvector', 'floating-point', 'SMT', 'SAT', 'compiler', 'equivalence']
FIELDS = ['id', 'full_name', 'html_url', 'description', 'language', 'stargazers_count',
          'forks_count', 'archived', 'disabled', 'fork', 'created_at', 'updated_at',
          'pushed_at', 'default_branch', 'size', 'homepage', 'topics', 'license']


def stamp(epoch):
    return dt.datetime.fromtimestamp(epoch, UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def save_json(path, data):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


class Collector:
    def __init__(self, out, cache):
        self.out, self.cache = out, cache
        out.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
        self.started = dt.datetime.now(UTC).isoformat()
        self.known_unretrievable = set()
        previous = out / 'progress.json'
        if previous.exists():
            previous_data = json.loads(previous.read_text())
            self.started = previous_data.get('started_at', self.started)
            for entry in previous_data.get('errors', []):
                match = re.search(r' created:(\S+)\.\.(\S+)$', entry.get('query', ''))
                if match and match[1] == match[2]:
                    self.known_unretrievable.add(int(dt.datetime.fromisoformat(match[1].replace('Z', '+00:00')).timestamp()))
        self.last_search = 0
        self.search_lock = threading.Lock()
        self.requests = 0
        self.cached = 0
        self.queries = []
        self.leaves = []
        self.repos = {}
        self.hits = {}
        self.errors = []

    def log(self, message):
        print(stamp(time.time()), message, flush=True)

    def api(self, endpoint):
        key = hashlib.sha256(endpoint.encode()).hexdigest()
        filename = self.cache / (key + '.json')
        if filename.exists():
            self.cached += 1
            return json.loads(filename.read_text())['data']
        for attempt in range(5):
            if endpoint.startswith('search/'):
                with self.search_lock:
                    time.sleep(max(0, 2.25 - (time.monotonic() - self.last_search)))
                    self.last_search = time.monotonic()
            result = subprocess.run(['gh', 'api', '--include', endpoint],
                                    text=True, capture_output=True, timeout=120)
            self.requests += 1
            if result.returncode == 0:
                raw = result.stdout.replace('\r\n', '\n')
                head, body = raw.split('\n\n', 1)
                data = json.loads(body)
                headers = {}
                for line in head.splitlines()[1:]:
                    if ':' in line:
                        name, value = line.split(':', 1)
                        headers[name.lower()] = value.strip()
                save_json(filename, {'endpoint': endpoint, 'retrieved_at': stamp(time.time()),
                                     'headers': headers, 'data': data})
                if headers.get('x-ratelimit-remaining') == '0':
                    delay = max(0, int(headers.get('x-ratelimit-reset', 0)) - time.time() + 2)
                    self.log(f'API quota window exhausted; waiting {delay:.0f}s')
                    time.sleep(min(delay, 3600))
                return data
            error = result.stderr.strip()[:600]
            self.log(f'API retry {attempt + 1}: {error}')
            time.sleep(65 if ('403' in error or '429' in error or 'rate limit' in error.lower()) else 5 * (attempt + 1))
        raise RuntimeError(f'API failed: {endpoint}: {error}')

    def search(self, query, page=1):
        endpoint = 'search/repositories?' + urllib.parse.urlencode({
            'q': query, 'sort': 'stars', 'order': 'desc', 'per_page': 100, 'page': page})
        data = self.api(endpoint)
        self.queries.append({'query': query, 'page': page,
                             'total_count': data['total_count'],
                             'incomplete_results': data['incomplete_results'],
                             'returned': len(data['items'])})
        return data

    def add(self, items, term=None):
        for item in items:
            row = {key: item.get(key) for key in FIELDS}
            self.repos[str(item['id'])] = row
            if term:
                self.hits.setdefault(item['full_name'], set()).add(term)

    def remaining_pages(self, query, count):
        pages = list(range(2, math.ceil(count / 100) + 1))
        if not pages:
            return []
        with ThreadPoolExecutor(max_workers=3) as pool:
            return list(pool.map(lambda page: self.search(query, page), pages))

    def partition(self, base, low, high, term=None, initial=None, depth=0):
        query = base + f' created:{stamp(low)}..{stamp(high)}'
        first = initial if initial is not None else self.search(query)
        count = first['total_count']
        if count > 900 or first['incomplete_results']:
            if high <= low:
                raise RuntimeError(f'Cannot safely partition {query}: {count}')
            middle = (low + high) // 2
            left = self.partition(base, low, middle, term, depth=depth + 1)
            right = self.partition(base, middle + 1, high, term, depth=depth + 1)
            return left | right
        items = first['items'][:]
        for page, data in enumerate(self.remaining_pages(query, count), start=2):
            if data['incomplete_results']:
                raise RuntimeError(f'Incomplete page: {query} page {page}')
            items.extend(data['items'])
        unique = {str(item['id']): item for item in items}
        if len(unique) != count:
            if high <= low:
                gap = {'kind': 'unretrievable_search_match', 'query': query,
                       'term': term, 'expected_count': count, 'returned_count': len(unique),
                       'missing_count': count - len(unique),
                       'explanation': 'GitHub search count does not match returned records at a single creation timestamp; see discrepancy-investigation.json.'}
                self.errors.append(gap)
                self.known_unretrievable.add(low)
                self.log(f'DOCUMENTED SEARCH DISCREPANCY: {len(unique)}/{count}: {query}')
            else:
                # Reuse a confirmed count-only timestamp for later keyword
                # queries, but recheck that this keyword actually matches it.
                # The gap remains its own explicit partition, preserving full
                # disjoint range coverage and honest expected/returned counts.
                if term:
                    for point in sorted(self.known_unretrievable):
                        if not low <= point <= high:
                            continue
                        point_query = base + f' created:{stamp(point)}..{stamp(point)}'
                        point_data = self.search(point_query)
                        if (not point_data['incomplete_results'] and
                                point_data['total_count'] > len(point_data['items'])):
                            result = self.partition(base, point, point, term, initial=point_data)
                            if low < point:
                                result |= self.partition(base, low, point - 1, term)
                            if point < high:
                                result |= self.partition(base, point + 1, high, term)
                            return result
                self.log(f'Repartitioning unstable page range: {len(unique)}/{count}')
                middle = (low + high) // 2
                return (self.partition(base, low, middle, term, depth=depth + 1)
                        | self.partition(base, middle + 1, high, term, depth=depth + 1))
        self.add(unique.values(), term)
        self.leaves.append({'base_query': base, 'query': query, 'term': term,
                            'expected_count': count, 'unique_count': len(unique),
                            'pages': math.ceil(count / 100)})
        if term:
            self.log(f'Keyword {term}: leaf {count}; matched repositories {len(self.hits)}')
        else:
            self.log(f'Census leaf {count}; total distinct repositories {len(self.repos)}')
        self.checkpoint()
        return set(unique)

    def checkpoint(self):
        save_json(self.out / 'progress.json', {
            'started_at': self.started, 'updated_at': stamp(time.time()),
            'distinct_repositories': len(self.repos), 'keyword_matched_repositories': len(self.hits),
            'network_requests': self.requests, 'cached_requests': self.cached,
            'completed_leaves': len(self.leaves), 'errors': self.errors})
        save_json(self.out / 'queries.json', self.queries)
        with (self.out / 'repositories.jsonl').open('w') as stream:
            for item in self.repos.values():
                stream.write(json.dumps(item, ensure_ascii=False) + '\n')
        save_json(self.out / 'keyword-hits.json', {k: sorted(v) for k, v in self.hits.items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=Path(__file__).parent)
    parser.add_argument('--cache', type=Path, default=Path('/tmp/llm2fpga-lean-survey-20261101/cache'))
    args = parser.parse_args()
    collector = Collector(args.out, args.cache)
    low = int(dt.datetime(2000, 1, 1, tzinfo=UTC).timestamp())
    high = int(dt.datetime(2026, 11, 1, 23, 59, 59, tzinfo=UTC).timestamp())
    root = collector.search('language:lean')
    collector.log(f'Initial GitHub language:lean total_count={root["total_count"]}')
    save_json(args.out / 'top100-original-search.json', root)
    census_ids = collector.partition('language:lean', low, high)
    save_json(args.out / 'census-ids.json', sorted(census_ids))
    collector.log(f'CENSUS COMPLETE: {len(census_ids)} repositories')
    for term in REQUESTED + SUPPLEMENTAL:
        base = f'language:lean {term} in:name,description,readme'
        result = collector.search(base)
        collector.log(f'Keyword {term}: initial count {result["total_count"]}')
        if result['total_count'] <= 900 and not result['incomplete_results']:
            items = result['items'][:]
            for data in collector.remaining_pages(base, result['total_count']):
                if data['incomplete_results']:
                    raise RuntimeError(f'Incomplete keyword result: {base}')
                items.extend(data['items'])
            unique = {str(item['id']): item for item in items}
            if len(unique) == result['total_count']:
                collector.add(unique.values(), term)
                collector.leaves.append({'base_query': base, 'query': base, 'term': term,
                                         'expected_count': result['total_count'],
                                         'unique_count': len(unique),
                                         'pages': math.ceil(result['total_count'] / 100)})
            else:
                collector.partition(base, low, high, term)
        else:
            collector.partition(base, low, high, term)
        collector.checkpoint()
    collector.checkpoint()
    save_json(args.out / 'coverage.json', {
        'started_at': collector.started, 'finished_at': stamp(time.time()),
        'scope': 'Public repositories returned by GitHub repository search language:lean; default excludes forks.',
        'initial_search_total': root['total_count'], 'census_unique_count': len(census_ids),
        'all_collected_unique_count': len(collector.repos),
        'keyword_matched_count': len(collector.hits),
        'recent_activity_threshold': '2026-09-01T00:00:00Z',
        'activity_definition': 'GitHub pushed_at; not a guarantee of default-branch substantive maintenance.',
        'requested_keywords': REQUESTED, 'supplemental_keywords': SUPPLEMENTAL,
        'screening_scope': 'All census metadata plus GitHub-indexed name, description and README keyword searches. Source review is separate, not exhaustive file-by-file review.',
        'pagination_policy': 'Disjoint creation-time ranges <=900 results; count mismatches and incomplete results are subdivided. Unretrievable count-only matches at single-second ranges are recorded explicitly as coverage gaps.',
        'live_index_caveat': 'GitHub search is mutable and not an atomic snapshot; inaccessible and unindexed repositories cannot be guaranteed.',
        'leaves': collector.leaves, 'network_requests': collector.requests,
        'errors': collector.errors})
    collector.log('COLLECTION COMPLETE')


if __name__ == '__main__':
    main()
