"""Stage ELF PT_LOAD segments using official pyelftools; no ELF parser here."""
from __future__ import annotations

import csv
from pathlib import Path

from bootstrap_elftools import reader, SHA256
from server_bundle import write
from yosys_hierarchy import digest


def stage(path, target):
    entries, ranges = [], []
    with path.open('rb') as stream:
        elf = reader()(stream)
        if elf.elfclass != 64 or not elf.little_endian or elf['e_machine'] != 'EM_RISCV' or elf['e_type'] != 'ET_EXEC':
            raise ValueError('Expected a little-endian RV64 executable ELF')
        if elf['e_entry'] != 0x10000000:
            raise ValueError('ELF must start at the original hardware reset vector')
        for segment in elf.iter_segments():
            if segment['p_type'] != 'PT_LOAD' or not segment['p_memsz']:
                continue
            address, size, memory_size = (segment[key] for key in ('p_paddr','p_filesz','p_memsz'))
            if size > memory_size or not any(low <= address and address+memory_size <= high for low, high in
                                          [(0x10000000,0x10001000),(0x80000000,0x88000000)]):
                raise ValueError('ELF load segment is outside the configured external ROM/DRAM')
            if any(address < high and low < address+memory_size for low, high in ranges):
                raise ValueError('Overlapping ELF load segments')
            ranges.append((address, address+memory_size))
            name = 'segment-'+str(len(entries))+'.bin'
            data = segment.data()
            if len(data) != size:
                raise ValueError('Truncated ELF segment')
            (target / name).write_bytes(data)
            entries.append({'file':name,'address':address,'file_size':size,'memory_size':memory_size,
                            'sha256':digest(target / name)})
    if not entries:
        raise ValueError('ELF contains no loadable segments')
    with (target / 'segments.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, ['file','address','file_size','memory_size'])
        writer.writeheader()
        writer.writerows({key: entry[key] for key in writer.fieldnames} for entry in entries)
    report = {'program_elf_sha256':digest(path),'pyelftools_wheel_sha256':SHA256,'segments':entries,
              'zero_fill':'External memory starts zero; each PT_LOAD file image is overlaid, preserving zero BSS.'}
    write(target / 'elf-image.json', report)
    return report
