"""Read-only monitors for the pinned Nanhu DefaultConfig Verilated hierarchy."""
from __future__ import annotations


def wire_observations():
    """Additional output taps and their read-only original-Verilator readers."""
    result = []
    prefix = 'dut.rootp->XSTop__DOT__core_with_l2__DOT__core__DOT__ctrlBlock__DOT__'
    def add(name, suffix, width, cpp=None):
        result.append({'name': 'c2l_observe_'+name, 'wire': 'core_with_l2.core.ctrlBlock.'+suffix,
                       'width': width, 'cpp': cpp or prefix+suffix})
    add('is_commit', '_rob_io_commits_isCommit', 1)
    add('flush', '_rob_io_flushOut_valid', 1)
    for lane in range(6):
        add(f'valid_{lane}', f'_rob_io_commits_commitValid_{lane}', 1)
        for key, width in [('ldest', 5), ('pdest', 8), ('rfWen', 1)]:
            add(f'{key}_{lane}', f'_rob_io_commits_info_{lane}_{key}', width)
        add(f'ftq_valid_{lane}', f'io_frontend_toFtq_rob_commits_{lane}_valid_REG', 1)
        for key, width in [('ftqIdx_value', 6), ('ftqOffset', 4)]:
            add(f'{key}_{lane}', f'io_frontend_toFtq_rob_commits_{lane}_bits_r_{key}', width)
    for bank in range(4):
        pointer = ('dut.rootp->__PVT__XSTop__DOT__core_with_l2__DOT__core__DOT__'
                   f'ctrlBlock__DOT__pcMem__DOT__dataBanks_{bank}')
        for row in range(16):
            for key, width in [('startAddr', 39), ('nextLineAddr', 39)]+[(f'isNextMask_{i}', 1) for i in range(16)]:
                add(f'pc_{bank*16+row}_{key}', f'pcMem.dataBanks_{bank}.data_{row}_{key}', width,
                    f'{pointer}->__PVT__data_{row}_{key}')
    add('flush_ftqIdx_value', 'flushRedirect_bits_r_ftqIdx_value', 6)
    add('flush_ftqOffset', 'flushRedirect_bits_r_ftqOffset', 4)
    return result


def pc_monitor():
    assignments = []
    for bank in range(4):
        pointer = ('dut.rootp->__PVT__XSTop__DOT__core_with_l2__DOT__core__DOT__'
                   f'ctrlBlock__DOT__pcMem__DOT__dataBanks_{bank}')
        for row in range(16):
            masks = ' | '.join(f'(uint16_t({pointer}->__PVT__data_{row}_isNextMask_{i}) << {i})' for i in range(16))
            assignments.append(f'  result[{bank*16+row}] = {{uint64_t({pointer}->__PVT__data_{row}_startAddr), '
                               f'uint64_t({pointer}->__PVT__data_{row}_nextLineAddr), uint16_t({masks})}};')
    return '''struct PcEntry { uint64_t start, nextLine; uint16_t isNext; };
std::array<PcEntry,64> snapshotPc(VXSTop& dut) {
  std::array<PcEntry,64> result;
''' + '\n'.join(assignments) + '''
  return result;
}
uint64_t retiredPc(const PcEntry& entry, unsigned offset) {
  if (offset >= 16) throw std::runtime_error("invalid FTQ offset");
  // Exact CtrlBlock.sv flushPC reconstruction, also used by its PC readers:
  // line bits select nextLine only for a second-line start and isNextMask.
  uint64_t line = ((entry.isNext >> offset) & 1) && ((entry.start >> 5) & 1)
                  ? entry.nextLine >> 6 : entry.start >> 6;
  uint64_t low = (((entry.start >> 1) & 31) + offset) & 31;
  uint64_t pc = (line << 6) | (low << 1);
  return (pc & (uint64_t(1) << 38)) ? pc | ~((uint64_t(1) << 39)-1) : pc;
}
'''
