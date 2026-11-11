"""Strict physical-port comparison for complete sampled RTL/Lean traces."""
from __future__ import annotations


def compare_rows(actual, reference, output_names, samples):
    names = set(output_names)
    if len(actual) != samples or len(reference) != samples:
        return {'status': 'INCOMPLETE', 'expected_samples': samples,
                'lean_samples': len(actual), 'rtl_samples': len(reference)}
    for index, (lean, rtl) in enumerate(zip(actual, reference)):
        if lean.get('cycle') != index or rtl.get('cycle') != index:
            return {'status': 'INVALID_CYCLE', 'first_sample': index}
        for phase in ('pre', 'post'):
            left, right = lean.get(phase, {}), rtl.get(phase, {})
            if set(left) != names or set(right) != names:
                return {'status': 'PORT_MISMATCH', 'first_sample': index, 'phase': phase,
                        'lean_missing': sorted(names-set(left)), 'rtl_missing': sorted(names-set(right)),
                        'lean_extra': sorted(set(left)-names), 'rtl_extra': sorted(set(right)-names)}
            differences = {name: {'lean': left[name], 'rtl': right[name]}
                           for name in sorted(names) if left[name] != right[name]}
            if differences:
                return {'status': 'MISMATCH', 'first_sample': index, 'phase': phase,
                        'differences': differences}
    return {'status': 'PASS', 'samples': samples, 'output_ports': len(names),
            'output_comparisons': samples * 2 * len(names)}
