"""Observe native compiler processes without issuing build acceptance.

Memory is the maximum sampled sum of Linux /proc RSS for the command and its
descendants, in KiB. Shared pages can be counted repeatedly; snapshots are not
atomic and short-lived peaks can be missed. External daemon workers and children
that detach before discovery are outside this accounting. This is an observed
RSS metric, not a cgroup memory peak or an estimate of allocated model storage.
"""
import json
import math
import os
from pathlib import Path
import re
import resource
import signal
import subprocess
import time

from .stages import digest, _write_json


def _processes():
    records = {}
    errors = 0
    for path in Path('/proc').iterdir():
        if not path.name.isdecimal():
            continue
        try:
            # The parenthesized comm field may itself contain spaces or ')'.
            fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
            records[int(path.name)] = {
                'state': fields[0], 'parent': int(fields[1]),
                'session': int(fields[3]), 'start': int(fields[19]),
                'rss_kib': max(0, int(fields[21])) * os.sysconf('SC_PAGE_SIZE') // 1024,
            }
        except (FileNotFoundError, ProcessLookupError):
            pass  # Exit between enumeration and read is normal.
        except (OSError, ValueError, IndexError):
            errors += 1
    return records, errors


def _sample_tree(root, known):
    records, errors = _processes()
    # start_new_session makes the session an ownership boundary, also after
    # the parent exits. Previously observed descendants retain birth identities
    # if they detach; PID reuse cannot attach an unrelated process to the tree.
    original = records.get(root)
    reused = original is not None and root in known and original['start'] != known[root]
    owned = {pid for pid, row in records.items()
             if (known.get(pid) == row['start'] or
                 (not reused and row['session'] == root))}
    while True:
        children = {pid for pid, row in records.items() if row['parent'] in owned}
        expanded = owned | children
        if expanded == owned:
            break
        owned = expanded
    for pid in owned:
        known[pid] = records[pid]['start']
    live = {pid: records[pid] for pid in owned if records[pid]['state'] not in ('Z', 'X')}
    return live, errors


def _authenticate(identities):
    for name, expected in identities.items():
        if digest(Path(name)) != expected:
            raise ValueError('dependency identity mismatch: ' + name)


def _terminate_tree(root, known):
    live, _ = _sample_tree(root, known)
    for pid, row in live.items():
        try:
            # A PID fd addresses the process itself, rather than a number that
            # may be reused after the sample. Recheck birth after opening it.
            descriptor = os.pidfd_open(pid)
            try:
                fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
                if int(fields[19]) == row['start']:
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            finally:
                os.close(descriptor)
        except (FileNotFoundError, ProcessLookupError):
            pass


def measure_stage(argv, output_dir, *, input_hashes, tool_hashes,
                  profile_sha256, timeout_seconds, sample_interval_seconds=0.05,
                  environment=None, cwd=None):
    """Retain auditable observations for one native invocation in a fresh folder.

The caller supplies its declared profile/input/tool identities. Files are
authenticated before and after execution. Exit zero is only process completion;
artifact correctness, numerical comparisons and fit belong to downstream gates.
The command runs in a private session so timeout cleanup cannot kill other jobs.
"""
    argv = tuple(argv)
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise ValueError('Linux PID fd support required for owned process cleanup')
    if (not argv or not Path(argv[0]).is_absolute() or argv[0] not in tool_hashes
            or not input_hashes or not re.fullmatch('[0-9a-f]{64}', profile_sha256)):
        raise ValueError('native command and profile/input/tool identities required')
    for value in (timeout_seconds, sample_interval_seconds):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError('measurement limits must be positive finite numbers')
    for identities in (input_hashes, tool_hashes):
        for name, identity in identities.items():
            if not Path(name).is_absolute() or not re.fullmatch('[0-9a-f]{64}', identity):
                raise ValueError('absolute dependencies and SHA-256 identities required')
    for name in input_hashes.keys() & tool_hashes.keys():
        if input_hashes[name] != tool_hashes[name]:
            raise ValueError('conflicting dependency identities: ' + name)
    output = Path(output_dir).resolve()
    if output.exists():
        raise ValueError('fresh measurement output directory required')
    output.mkdir(parents=True)
    work = Path(cwd).resolve() if cwd is not None else output
    identities = {**input_hashes, **tool_hashes}
    collector = Path(__file__).resolve()
    collector_identity = digest(collector)
    identities.setdefault(str(collector), collector_identity)
    limits = {
        'timeout_seconds': timeout_seconds,
        'inherited_address_space_soft_bytes': None,
        'inherited_address_space_hard_bytes': None,
        'address_space_scope': 'inherited per-process RLIMIT_AS; no aggregate memory limit',
    }
    for key, limit in zip(('inherited_address_space_soft_bytes', 'inherited_address_space_hard_bytes'),
                          resource.getrlimit(resource.RLIMIT_AS)):
        limits[key] = None if limit == resource.RLIM_INFINITY else limit
    specification = dict(argv=argv, cwd=str(work), profile_sha256=profile_sha256,
                         input_hashes=input_hashes, tool_hashes=tool_hashes,
                         collector_sha256=collector_identity, limits=limits,
                         sample_interval_seconds=sample_interval_seconds,
                         environment=environment or {})
    _write_json(output / 'measurement-spec.json', specification)
    started = time.monotonic()
    status, diagnostic, exit_code = 'failed', '', None
    authenticated = False
    samples, peak, peak_processes, sampling_errors = 0, None, 0, 0
    process, known = None, {}
    try:
        _authenticate(identities)
        authenticated = True
        with (output / 'stdout.log').open('wb') as stdout, (output / 'stderr.log').open('wb') as stderr, \
                (output / 'memory-samples.jsonl').open('w') as trace:
            process = subprocess.Popen(argv, cwd=work, env={**os.environ, **(environment or {})},
                                       stdout=stdout, stderr=stderr, start_new_session=True)
            while True:
                live, errors = _sample_tree(process.pid, known)
                sampling_errors += errors
                elapsed = time.monotonic() - started
                if live:
                    rss = sum(row['rss_kib'] for row in live.values())
                    samples += 1
                    peak = max(peak or 0, rss)
                    peak_processes = max(peak_processes, len(live))
                    trace.write(json.dumps({'elapsed_seconds': elapsed, 'rss_kib': rss,
                                            'processes': len(live)}) + '\n')
                    trace.flush()
                exit_code = process.poll()
                if exit_code is not None and not live:
                    break
                if elapsed >= timeout_seconds:
                    status = 'timeout'
                    diagnostic = 'native process tree exceeded wall-time limit'
                    _terminate_tree(process.pid, known)
                    exit_code = process.wait()
                    break
                time.sleep(min(sample_interval_seconds, max(0, timeout_seconds - elapsed)))
        if status != 'timeout':
            status = 'completed' if exit_code == 0 else 'failed'
        _authenticate(identities)
    except ValueError as error:
        authenticated = False
        status, diagnostic = 'dependency-error', str(error)
    except OSError as error:
        status, diagnostic = 'failed', str(error)
    finally:
        if process is not None and process.poll() is None:
            _terminate_tree(process.pid, known)
            exit_code = process.wait()
        # Before-execution rejection still retains empty raw streams.
        for name in ('stdout.log', 'stderr.log', 'memory-samples.jsonl'):
            (output / name).touch(exist_ok=True)
    report = dict(
        schema_version=1, scope='native process observations only; no build/numerical/fit acceptance',
        status=status, diagnostic=diagnostic, exit_code=exit_code,
        signal=-exit_code if exit_code is not None and exit_code < 0 else None,
        elapsed_seconds=time.monotonic() - started, inputs_authenticated=authenticated,
        profile_sha256=profile_sha256, peak_memory_kib=peak,
        peak_memory_method='maximum sampled sum of process-tree Linux procfs RSS',
        sample_interval_seconds=sample_interval_seconds, memory_samples=samples,
        peak_processes=peak_processes, sampling_errors=sampling_errors, limits=limits,
        limitations=['shared pages may be counted repeatedly', 'snapshots are not atomic',
                     'short-lived peaks and undiscovered detached children may be missed',
                     'external daemon workers are not accounted'],
        raw_logs={name: str(output / name) for name in
                  ('stdout.log', 'stderr.log', 'memory-samples.jsonl')},
        measurement_spec_sha256=digest(output / 'measurement-spec.json'))
    _write_json(output / 'measurement.json', report)
    return report
