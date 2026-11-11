#!/usr/bin/env python3
"""Linux research adapter around the pinned xiangshan2Lean helpers.

Upstream evidence remains byte-for-byte unchanged. The only platform shim
replaces its Windows-only ijson loader with the installed Linux wheel.
No operation semantics or initialization rules are replaced here.
"""

import argparse
import gc
import hashlib
import json
from pathlib import Path
import resource
import sys
import time

sys.dont_write_bytecode = True


def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['audit', 'import'])
    parser.add_argument('--upstream', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--ijson', type=Path, default=Path('/tmp/llm2fpga-ijson'))
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    sys.path[:0] = [str(args.ijson), str(args.upstream / 'scripts')]
    import ijson
    import yosys_json
    from functional_ir import import_functional_file
    from lean_table import emit_table

    yosys_json.reader = lambda: ijson
    started = time.monotonic()
    result = {'stage': args.stage, 'upstream_commit': 'a1299b564831e571b9b80f816219ae06c91db342',
              'python': sys.version, 'ijson_version': ijson.__version__,
              'platform_adaptation': 'Linux ijson loader; upstream helpers unchanged',
              'all_input_equivalence_proved': False}
    try:
        if args.stage == 'audit':
            clock, hardware, audit = yosys_json.audit(args.work / 'before.json', 'model_top',
                'clk', False, 'global')
            result.update(clock=clock, hardware=hardware, audit=audit)
            if audit['issues']:
                result['status'] = 'requires_support'
            else:
                result['status'] = 'completed'
        else:
            from transfer_projection import project
            transfer = args.work / 'transfer.smt2'
            result['projection'] = project(args.work / 'functional.smt2', transfer, 'model_top')
            print(json.dumps(result['projection']), flush=True)
            metadata, effects, arbitrary = yosys_json.expose(args.work / 'named.json', None,
                'model_top', False, [], everted=True)
            result['arbitrary_inputs'] = arbitrary
            print('Matching initialization metadata loaded.', flush=True)
            ir = import_functional_file(transfer, metadata,
                'TinyStoriesFull', None, compact_nodes=True)
            del metadata
            gc.collect()
            ir['clock_mode'] = 'global'
            ir['arbitrary_inputs'] = arbitrary
            ir['lean_backend'] = 'table'
            result['imported'] = {'nodes': len(ir['nodes']), 'inputs': len(ir['inputs']),
                'states': len(ir['states']), 'outputs': len(ir['outputs']),
                'memory_states': sum(field['sort']['kind'] == 'memory' for field in ir['states']),
                'node_storage': type(ir['nodes']).__name__}
            print(json.dumps(result['imported']), flush=True)
            # Names/widths suffice for the retained interface. All initial values
            # remain in the exact binary/hex table checked by the Lean decoder.
            interface = {kind: [{key: value for key, value in field.items() if key != 'initial'}
                                for field in ir[kind]] for kind in ['inputs', 'states', 'outputs']}
            (args.work / 'interface.json').write_text(json.dumps(interface, indent=2) + '\n')
            runtime = args.upstream / 'lean/Chisel2Lean/Functional.lean'
            lean, table = emit_table(ir, args.work, runtime_source=runtime.read_bytes())
            result['table'] = table
            result['generated_lean'] = str(lean)
            result['status'] = 'completed'
            for name in ['TinyStoriesFull.lean', table['runtime'], table['binary'], table['hexadecimal']]:
                path = args.work / name
                result.setdefault('artifacts', []).append({'path': str(path),
                    'bytes': path.stat().st_size, 'sha256': sha(path)})
    except Exception as error:
        import traceback
        result.update(status='failed', error_type=type(error).__name__, error=str(error),
                      traceback=traceback.format_exc())
    result['seconds'] = time.monotonic() - started
    result['peak_rss_kib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    args.report.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2), flush=True)
    raise SystemExit(0 if result['status'] == 'completed' else 1)
