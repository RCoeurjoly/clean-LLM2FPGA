#!/usr/bin/env python3
"""Whole-artifact TorchLean frontend coverage spike; no Lean proof is claimed."""

import argparse
from collections import Counter, defaultdict
import csv
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import resource
import time
import traceback

import torch
import torch.ao.quantization.fx._decomposed  # Register saved quantized operators.


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, default=str) + '\n')


def run(model_path, adapter_path, destination):
    destination.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(1)
    spec = importlib.util.spec_from_file_location('capture_adapter', adapter_path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    report = {'model_path': str(model_path.resolve()), 'model_sha256': sha256(model_path),
              'model_bytes': model_path.stat().st_size, 'torch': torch.__version__,
              'python': platform.python_version(), 'adapter_sha256': sha256(adapter_path),
              'torchlean_commit': 'f37cbc5aca8e79943b0ef0e3d3499ca9719d7c2b',
              'adapter_reconstructed_without_lean': True, 'lean_checker_run': False,
              'frontend_equivalence_proved': False, 'stages': {}}
    report['runtime'] = {'threads': torch.get_num_threads(),
                         'cpu_capability': torch.backends.cpu.get_cpu_capability(),
                         'quantized_engine': torch.backends.quantized.engine,
                         'mkldnn_enabled': torch.backends.mkldnn.enabled,
                         'deterministic_algorithms': torch.are_deterministic_algorithms_enabled()}

    def stage(name, action):
        start = time.monotonic()
        try:
            result = action()
            report['stages'][name] = {'status': 'completed', 'seconds': time.monotonic() - start}
            return result
        except Exception as error:
            trace = traceback.format_exc()
            report['stages'][name] = {'status': 'failed', 'seconds': time.monotonic() - start,
                                      'type': type(error).__name__, 'message': str(error)}
            (destination / (name + '.traceback.txt')).write_text(trace)
            return None
        finally:
            write_json(destination / 'report.json', report)

    program = stage('load', lambda: torch.export.load(model_path))
    if program is None:
        return report
    module = program.module()
    inputs, kwargs = program.example_inputs
    report['example_inputs'] = [{'shape': list(x.shape), 'dtype': str(x.dtype)} for x in inputs]
    report['example_kwargs'] = kwargs
    report['state_tensors'] = len(program.state_dict)
    report['state_elements'] = sum(x.numel() for x in program.state_dict.values())
    report['graph_nodes'] = len(list(program.graph.nodes))
    report['node_kinds'] = dict(Counter(n.op for n in program.graph.nodes))
    report['operator_counts'] = dict(Counter(str(n.target) for n in program.graph.nodes
                                             if n.op.startswith('call_')))
    report['range_constraints'] = str(program.range_constraints)
    users = {entry.arg.name for entry in program.graph_signature.input_specs
             if entry.kind.name == 'USER_INPUT'}
    dependencies = {}
    for node in program.graph.nodes:
        dependencies[node] = node.name in users or any(dependencies[parent]
                                                       for parent in node.all_input_nodes)
    linears = [node for node in program.graph.nodes if str(node.target) == 'aten.linear.default']
    report['linear_weight_dependencies'] = dict(Counter(
        'user_input_dependent' if dependencies[node.args[1]] else 'frozen_state_only'
        for node in linears))
    report['dropout_arguments'] = dict(Counter(str(node.args[1:]) for node in program.graph.nodes
                                               if str(node.target) == 'aten.dropout.default'))
    report['tensor_node_dtypes'] = dict(Counter(str(node.meta['val'].dtype)
        for node in program.graph.nodes if isinstance(node.meta.get('val'), torch.Tensor)))
    (destination / 'original_graph.py.txt').write_text(program.graph_module.code)

    def execute():
        with torch.no_grad():
            output = module(*inputs, **kwargs)
        leaves, _ = torch.utils._pytree.tree_flatten(output)
        report['sample_outputs'] = [{'shape': list(x.shape), 'dtype': str(x.dtype),
                                     'finite': bool(torch.isfinite(x).all()),
                                     'sha256': hashlib.sha256(x.contiguous().view(torch.uint8)
                                                             .numpy().tobytes()).hexdigest()}
                                    for x in leaves]
    stage('reference_execution', execute)

    def whole_import():
        with torch.no_grad():
            artifact = adapter.export_torchlean_graph_json(
                module, inputs, str(destination / 'whole_graph.json'), require_torch_export=True)
        report['imported_nodes'] = len(artifact['nodes'])
    stage('whole_frontend_import', whole_import)

    def scan():
        adapter._TENSOR_VALUES = {}
        state = dict(program.state_dict)
        constants = dict(program.constants)
        for entry in program.graph_signature.input_specs:
            if entry.kind.name != 'USER_INPUT':
                value = state.get(entry.target, constants.get(entry.target))
                if isinstance(value, torch.Tensor):
                    adapter._TENSOR_VALUES[entry.arg.name] = value
        helpers = adapter._routing_helpers(program.graph)
        rows = []
        groups = defaultdict(lambda: {'count': 0, 'nodes': []})
        for node in program.graph.nodes:
            if node.op in ('placeholder', 'get_attr', 'output'):
                continue
            row = {'name': node.name, 'operator': str(node.target), 'fx_kind': node.op,
                   'shape': adapter._node_shape(node), 'argument_text': str(node.args),
                   'kwargs': str(node.kwargs)}
            value = node.meta.get('val')
            row['dtype'] = str(getattr(value, 'dtype', None))
            try:
                if node in helpers or adapter._zeros_like(node):
                    row['status'] = 'recognized_helper'
                elif adapter._metadata_constraint(node) is not None:
                    row['status'] = 'recognized_metadata'
                else:
                    kind = adapter._tuple_kind(node, module) if adapter._tuple_shapes_of_node(node) \
                        else adapter._lower_kind(node, module)
                    row['status'] = 'recognized_lowering'
                    row['lowered_kind'] = str(kind.get('kind', '_plan'))
            except Exception as error:
                row.update(status='unsupported', error_type=type(error).__name__, error=str(error))
            key = (row['operator'], row['status'], row.get('error', ''))
            groups[key]['count'] += 1
            groups[key]['nodes'].append(node.name)
            rows.append(row)
        write_json(destination / 'nodes.json', rows)
        table = [{'operator': op, 'status': status, 'error': error, 'count': item['count'],
                  'first_node': item['nodes'][0]}
                 for (op, status, error), item in sorted(groups.items())]
        with (destination / 'operator-coverage.csv').open('w') as output:
            writer = csv.DictWriter(output, fieldnames=list(table[0]))
            writer.writeheader()
            writer.writerows(table)
        report['coverage'] = {'nodes_by_status': dict(Counter(row['status'] for row in rows)),
                              'unique_operators': len(set(row['operator'] for row in rows)),
                              'unsupported_operator_identities': sorted(set(row['operator'] for row in rows
                                                                           if row['status'] == 'unsupported')),
                              'scope': 'lowering recognition only, not serialization/type/numerical proof'}
    stage('scan_original_graph', scan)
    report['peak_rss_kib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    write_json(destination / 'report.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('model', type=Path)
    parser.add_argument('--adapter', type=Path, default=Path(__file__).with_name('capture_adapter.py'))
    parser.add_argument('--out', type=Path, default=Path(__file__).with_name('results'))
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.model, arguments.adapter, arguments.out), indent=2))
