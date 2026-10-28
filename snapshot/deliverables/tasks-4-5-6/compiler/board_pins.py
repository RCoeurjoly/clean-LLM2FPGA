"""Read the open CH0 UCF into typed nine-lane LiteDRAM pad inputs.

This is an input parser, not a transformation of generated HDL. It accepts
only the pin-assignment subset used by the pinned board source. Unknown nets,
malformed statements and incomplete or duplicated assignments are errors.
"""
import argparse
import json
from pathlib import Path
import re

from compiler.litedram_export import PIN_COUNTS, SCALAR_PINS, validate_pins


ASSIGNMENT = re.compile(r'NET\s+"([^"\s]+)"\s+LOC\s*=\s*"([A-Za-z0-9]+)"\s*(?:\|\s*)?;')
INDEXED = re.compile(r'ddr3_([a-z_]+)\[([0-9]+)\]')
NAMES = {'addr': 'a', 'ck_p': 'clk_p', 'ck_n': 'clk_n'}


def read_ddr_pins(source):
    indexed = {key: {} for key in PIN_COUNTS}
    scalars = {}
    for number, line in enumerate(Path(source).read_text().splitlines(), 1):
        statement = line.split('#', 1)[0].strip()
        if not statement:
            continue
        match = ASSIGNMENT.fullmatch(statement)
        if match is None:
            raise ValueError(f'malformed UCF pin assignment on line {number}')
        net, pin = match.groups()
        vector = INDEXED.fullmatch(net)
        if vector:
            name, index = vector.groups()
            name, index = NAMES.get(name, name), int(index)
            if name not in indexed or index >= PIN_COUNTS[name]:
                raise ValueError(f'unknown or out-of-range board net {net}')
            if index in indexed[name]:
                raise ValueError(f'duplicate board net {net}')
            indexed[name][index] = pin
        else:
            name = net.removeprefix('ddr3_')
            if net != 'ddr3_' + name or name not in SCALAR_PINS:
                raise ValueError(f'unknown board net {net}')
            if name in scalars:
                raise ValueError(f'duplicate board net {net}')
            scalars[name] = pin
    pins = dict(scalars)
    for name, count in PIN_COUNTS.items():
        if set(indexed[name]) != set(range(count)):
            raise ValueError(f'incomplete {name} board pins')
        pins[name] = [indexed[name][index] for index in range(count)]
    return validate_pins(pins)


def export_board_pins_stage(ucf, output, *, python):
    from compiler import litedram_export, stages
    output = Path(output).resolve()
    if Path(ucf).resolve().is_relative_to(output):
        raise ValueError('board pin output overlaps its input')
    (output / 'success.json').unlink(missing_ok=True)
    ucf = Path(ucf).resolve(strict=True)
    python = Path(python).absolute()
    module = Path(__file__).resolve()
    inputs = [ucf, module, Path(litedram_export.__file__).resolve(),
              Path(stages.__file__).resolve()]
    return stages.run_stage(stages.StageSpec(
        'graph-board-pins',
        (str(python), '-m', 'compiler.board_pins', '--ucf', str(ucf),
         '--output', 'pins.json'),
        {str(path): stages.digest(path) for path in inputs},
        {str(python): stages.digest(python)},
        {'channel': 0, 'byte_lanes': 9, 'scope': 'open board pad metadata only'},
        ('pins.json',),
        environment={'PYTHONPATH': str(module.parent.parent)}), output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ucf', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    pins = read_ddr_pins(args.ucf)
    args.output.write_text(json.dumps(pins, sort_keys=True, indent=2) + '\n')


if __name__ == '__main__':
    main()
