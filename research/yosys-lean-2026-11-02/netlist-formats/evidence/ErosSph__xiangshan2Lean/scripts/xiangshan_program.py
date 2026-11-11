"""Run bare-metal code on the original saved XSTop with real AXI responses.

The hardware archive is read-only. Only a new testbench is compiled and linked
to the original Verilated circuit; every input/output sample is recorded.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from common import ROOT, run_logged
from server import doctor, load_work, stamp
from server_bundle import MANIFEST, read, verify_bundle, write
from yosys_hierarchy import digest
from xiangshan_observer import pc_monitor, wire_observations


def harness(metadata, cycles, observe=False, dram_text=False, irq_on_read_wait=False):
    ports = metadata['ports']
    inputs = [p for p in ports if p['direction'] == 'input']
    outputs = [p for p in ports if p['direction'] == 'output']
    zero = '\n'.join(f'  dut.{p["rtl_name"]} = 0;' if p['width'] <= 64 else
                     f'  for (size_t i=0; i<{(p["width"]+31)//32}; ++i) dut.{p["rtl_name"]}[i] = 0;' for p in inputs)
    def value(p):
        return (f'hexWords(dut.{p["rtl_name"]}.data(), {(p["width"]+31)//32})' if p['width'] > 64 else
                f'std::to_string(uint64_t(dut.{p["rtl_name"]}))')
    record_inputs = '\n'.join(f"    inputTrace << ',' << {value(p)};" for p in inputs)
    record_outputs = '\n'.join(f"    outputTrace << ',' << {value(p)};" for p in outputs)
    observations = wire_observations() if observe else []
    observation_setup = ('  std::ofstream observationTrace("observations.csv");\n  observationTrace << "sample,' +
                         ','.join(p['name'] for p in observations) + '\\n";') if observe else ''
    observation_record = ('    observationTrace << (sample-1);\n' +
                          '\n'.join(f"    observationTrace << ',' << uint64_t({p['cpp']});" for p in observations) +
                          "\n    observationTrace << '\\n';") if observe else ''
    channels = []
    for prefix, size, variable in [('peripheral_0', 8, 'peripheral'), ('memory_0', 32, 'dram')]:
        # Explicit port adaptation to the actual original 64/256-bit interfaces.
        p = 'dut.' + prefix
        channels.append((f'''
    {variable}.drive(cycle);
    {p}_ar_ready = !dut.io_reset && {variable}.arready();
    {p}_aw_ready = !dut.io_reset && {variable}.awready();
    {p}_w_ready = !dut.io_reset && {variable}.wready();
    {p}_r_valid = {variable}.rvalid;
    {p}_r_bits_id = {variable}.rid;
    {p}_r_bits_resp = 0;
    {p}_r_bits_last = {variable}.rlast;
    {p}_b_valid = {variable}.bvalid;
    {p}_b_bits_id = {variable}.bid;
    {p}_b_bits_resp = 0;
''' + (f'    {p}_r_bits_data = pack64({variable}.rdata);\n' if size == 8 else
       f'    packWide({variable}.rdata, {p}_r_bits_data.data());\n'), f'''
    AxiSlave<{size}>::Sample {variable}Sample;
    {variable}Sample.ar = {p}_ar_valid && {p}_ar_ready;
    {variable}Sample.aw = {p}_aw_valid && {p}_aw_ready;
    {variable}Sample.w = {p}_w_valid && {p}_w_ready;
    {variable}Sample.r = {p}_r_valid && {p}_r_ready;
    {variable}Sample.b = {p}_b_valid && {p}_b_ready;
    {variable}Sample.readAddress = {{{p}_ar_bits_id, {p}_ar_bits_addr, {p}_ar_bits_len, {p}_ar_bits_size, {p}_ar_bits_burst}};
    {variable}Sample.writeAddress = {{{p}_aw_bits_id, {p}_aw_bits_addr, {p}_aw_bits_len, {p}_aw_bits_size, {p}_aw_bits_burst}};
    {variable}Sample.writeData.strobe = {p}_w_bits_strb;
    {variable}Sample.writeData.last = {p}_w_bits_last;
''' + (f'    unpack64({p}_w_bits_data, {variable}Sample.writeData.data);\n' if size == 8 else
       f'    unpackWide({p}_w_bits_data.data(), {variable}Sample.writeData.data);\n'),
       f'    {variable}.edge({variable}Sample, busTrace, "{variable}");'))
    commit_prefix = 'dut.rootp->XSTop__DOT__core_with_l2__DOT__core__DOT__ctrlBlock__DOT___rob_io_commits_'
    registers = 'dut.rootp->XSTop__DOT__core_with_l2__DOT__core__DOT__exuBlocks__DOT__scheduler__DOT__intRfReadData_regfile__DOT__mem_ext__DOT__Memory'
    csr = 'dut.rootp->XSTop__DOT__core_with_l2__DOT__core__DOT__exuBlocks__DOT__fuBlock__DOT__exeUnits_6__DOT__csr__DOT__'
    ctrl = 'dut.rootp->XSTop__DOT__core_with_l2__DOT__core__DOT__ctrlBlock__DOT__'
    irq_setup = '''
  peripheral.delayedReadAddress=0x50000000; peripheral.extraReadLatency=500;
  for (unsigned i=0;i<8;++i) memory.bytes[0x50000000+i]=uint64_t(0x12345678)>>(8*i);
''' if irq_on_read_wait else ''
    irq_drive = '''
    if (!irqInjected && !peripheral.rvalid) {
      for (const auto& request : peripheral.reads) if (request.address==0x50000000 && request.due>cycle) {
        irqInjected=true;
        eventTrace << cycle << ",assert_during_read_wait," << committed << ',' << request.address << ',' << request.due << '\\n';
        break;
      }
    }
    dut.io_extIntrs=irqInjected ? 1 : 0;
''' if irq_on_read_wait else ''
    irq_accept = f'''
    if (uint64_t({csr}mcause)==0x800000000000000bULL && !irqAccepted) {{
      if (!irqInjected) throw std::runtime_error("External interrupt accepted before test input");
      irqAccepted=true;
      eventTrace << cycle << ",interrupt_accepted," << committed << ',' << uint64_t({csr}mcause)
                 << ',' << uint64_t({csr}mepc) << '\\n';
    }}
''' if irq_on_read_wait else ''
    capture_commit = '\n'.join(f'''    commits[{i}] = {{bool({commit_prefix}isCommit && {commit_prefix}commitValid_{i}),
      unsigned({commit_prefix}info_{i}_ldest), unsigned({commit_prefix}info_{i}_pdest), unsigned({commit_prefix}info_{i}_rfWen), 0}};
    if (commits[{i}].valid && commits[{i}].rfwen && commits[{i}].ldest) {{
      if (commits[{i}].pdest >= 192) throw std::runtime_error("physical GPR destination out of range");
      commits[{i}].data = {registers}[commits[{i}].pdest];
    }}''' for i in range(6))
    commit = '\n'.join(f'''    if (bool({ctrl}io_frontend_toFtq_rob_commits_{i}_valid_REG) != (commits[{i}].valid && !flushBeforeEdge))
      throw std::runtime_error("FTQ retirement register alignment mismatch");
    if (commits[{i}].valid) {{
      if (flushBeforeEdge && {i} != 0) throw std::runtime_error("unexpected multi-lane retirement during ROB flush");
      unsigned ftq = flushBeforeEdge ? {ctrl}flushRedirect_bits_r_ftqIdx_value : {ctrl}io_frontend_toFtq_rob_commits_{i}_bits_r_ftqIdx_value;
      unsigned offset = flushBeforeEdge ? {ctrl}flushRedirect_bits_r_ftqOffset : {ctrl}io_frontend_toFtq_rob_commits_{i}_bits_r_ftqOffset;
      const auto& entry = commits[{i}];
      commitTrace << cycle << ",{i}," << entry.ldest << ',' << entry.pdest << ',' << entry.rfwen
                  << ',' << entry.data << ',' << retiredPc(pcBeforeEdge.at(ftq), offset) << ',' << ftq << ',' << offset << '\\n';
      ++committed;
    }}''' for i in range(6))
    return f'''#include "VXSTop.h"
#include "VXSTop___024root.h"
#include "VXSTop_DataModule_CtrlPcMem_16entry.h"
#include "verilated.h"
#include "axi_memory.h"
#include <iomanip>
#include <iostream>
#include <sstream>
{pc_monitor()}
std::string hexWords(const WData* data, size_t count) {{
  std::ostringstream out; out << "0x" << std::hex << std::setfill('0');
  while (count) out << std::setw(8) << data[--count];
  return out.str();
}}
template <size_t N> uint64_t pack64(const std::array<uint8_t,N>& data) {{
  uint64_t value = 0; for (size_t i=0;i<N;++i) value |= uint64_t(data[i]) << (i*8); return value;
}}
template <size_t N> void packWide(const std::array<uint8_t,N>& data, WData* out) {{
  for (size_t i=0;i<N/4;++i) {{ out[i]=0; for(size_t j=0;j<4;++j) out[i] |= uint32_t(data[i*4+j]) << (j*8); }}
}}
template <size_t N> void unpack64(uint64_t data, std::array<uint8_t,N>& out) {{
  for(size_t i=0;i<N;++i) out[i] = data >> (i*8);
}}
template <size_t N> void unpackWide(const WData* data, std::array<uint8_t,N>& out) {{
  for(size_t i=0;i<N;++i) out[i] = data[i/4] >> ((i%4)*8);
}}
int main(int argc,char** argv) {{ try {{
  Verilated::commandArgs(argc,argv); Verilated::fatalOnError(false);
  ByteMemory memory; {('memory.loadSegments("segments.csv");' if dram_text else 'memory.load("program.bin", 0x10000000);')}
  AxiSlave<8> peripheral(memory); AxiSlave<32> dram(memory);
{irq_setup}
  VXSTop dut;
{zero}
  dut.eval();
  if (Verilated::gotError()) return 7;
  std::ofstream inputTrace("inputs.csv"), outputTrace("outputs.csv"), busTrace("axi.csv"), commitTrace("commits.csv");
  inputTrace << "sample,{','.join(p['name'] for p in inputs)}\\n";
  outputTrace << "sample,{','.join(p['name'] for p in outputs)}\\n";
  busTrace << "cycle,channel,event,id,address,detail0,detail1\\n";
  commitTrace << "cycle,lane,ldest,pdest,rfwen,data,pc,ftqidx,ftqoffset\\n";
{observation_setup}
  uint64_t sample = 0, committed = 0, signatureCycle = 0;
  bool irqInjected=false, irqAccepted=false;
  std::ofstream eventTrace("events.csv");
  eventTrace << "cycle,event,committed,value,detail\\n";
  auto eval = [&]() {{
    inputTrace << sample;
{record_inputs}
    inputTrace << '\\n';
    Verilated::timeInc(1); dut.eval();
    if (Verilated::gotError()) throw std::runtime_error("Original RTL assertion failed at sample " + std::to_string(sample));
    outputTrace << sample++;
{record_outputs}
    outputTrace << '\\n';
{observation_record}
  }};
  for (uint64_t cycle=0; cycle<{cycles}; ++cycle) {{
    dut.io_clock=0; dut.io_reset=cycle<32; dut.io_pll0_lock=1;
    dut.io_systemjtag_reset=cycle<32; dut.io_systemjtag_jtag_TMS=1;
{irq_drive}
{''.join(c[0] for c in channels)}
    eval(); eval();
{''.join(c[1] for c in channels)}
    struct Commit {{ bool valid; unsigned ldest,pdest,rfwen; uint64_t data; }};
    std::array<Commit,6> commits;
    bool flushBeforeEdge = {ctrl}_rob_io_flushOut_valid;
    auto pcBeforeEdge = snapshotPc(dut);
{capture_commit}
    dut.io_clock=1; eval();
{commit}
{irq_accept}
    dut.io_clock=0; eval();
{''.join(c[2] for c in channels)}
    if (memory.signature && !signatureCycle) signatureCycle=cycle;
    if (cycle%1000==0) std::cerr << "cycle=" << cycle << " commits=" << committed << " peripheral_reads=" << peripheral.readRequests << " dram_reads=" << dram.readRequests << std::endl;
    bool dramIdle = dram.reads.empty() && dram.writes.empty() && dram.writeData.empty() && dram.responses.empty() && !dram.rvalid && !dram.bvalid;
    bool peripheralIdle = peripheral.reads.empty() && peripheral.writes.empty() && peripheral.writeData.empty() && peripheral.responses.empty() && !peripheral.rvalid && !peripheral.bvalid;
    if (signatureCycle && cycle>=signatureCycle+32 && peripheralIdle && dramIdle) {{
      if ({'true' if irq_on_read_wait else 'false'} && (!irqInjected || !irqAccepted))
        throw std::runtime_error("Interrupt/read-wait scenario did not occur");
      memory.dump("external-memory.csv");
      std::ofstream result("execution.json");
      result << "{{\\"status\\":\\"" << (memory.signature==1 && committed ? "RTL_PROGRAM_COMPLETED" : "PROGRAM_FAILED")
             << "\\",\\"cycles\\":" << cycle+1 << ",\\"samples\\":" << sample << ",\\"signature\\":" << memory.signature
             << ",\\"committed\\":" << committed << ",\\"peripheral_reads\\":" << peripheral.readRequests << ",\\"dram_reads\\":" << dram.readRequests
             << ",\\"dram_writes\\":" << dram.writeRequests << ",\\"dram_read_beats\\":" << dram.readBeats << ",\\"dram_write_beats\\":" << dram.writeBeats << "}}\\n";
      dut.final(); return memory.signature==1 && committed ? 0 : 9;
    }}
  }}
  std::cerr << "Program timed out: commits=" << committed << " signature=" << memory.signature << std::endl;
  std::cerr << "mcause=" << uint64_t({csr}mcause) << " mepc=" << uint64_t({csr}mepc) << " mtval=" << uint64_t({csr}mtval) << std::endl;
  dut.final(); return 8;
}} catch (const std::exception& e) {{ std::cerr << e.what() << std::endl; return 3; }} }}
'''


def run(root, work, cycles, observe=False, program_source=None, dram_text=False, irq_on_read_wait=False):
    bundle, _ = verify_bundle(root)
    load_work(root, work)
    host = doctor(True)
    execution = work / 'programs' / stamp()
    execution.mkdir(parents=True)
    print('PROGRAM_DIRECTORY=' + str(execution), flush=True)
    rtl = work / 'rtl-input'
    original_sources = read(root / 'model/origin-manifest.json')['source_files_sha256']
    for name, expected in original_sources.items():
        if digest(rtl / name) != expected:
            raise ValueError('Original working RTL changed: ' + name)
    source = ROOT / 'verification/xiangshan/programs'
    shutil.copy2(source / 'axi_memory.h', execution / 'axi_memory.h')
    shutil.copy2(source / ('link-dram.ld' if dram_text else 'link.ld'), execution / 'link.ld')
    if dram_text:
        shutil.copy2(source / 'boot.S', execution / 'boot.S')
    chosen = program_source.resolve() if program_source else source / 'smoke.S'
    if not chosen.is_file() or chosen.suffix not in {'.S', '.s'}:
        raise ValueError('Program source must be a RISC-V assembly file')
    shutil.copy2(chosen, execution / 'smoke.S')
    gcc = shutil.which('riscv64-unknown-elf-gcc')
    objcopy = shutil.which('riscv64-unknown-elf-objcopy')
    if not gcc or not objcopy:
        raise RuntimeError('Install a RISC-V bare-metal GNU toolchain on the server')
    command = [gcc, '-march=rv64gc', '-mabi=lp64d', '-nostdlib', '-nostartfiles',
               '-Wl,--no-relax', '-T', 'link.ld', *(['boot.S'] if dram_text else []), 'smoke.S', '-o', 'program.elf']
    if run_logged(command, execution / 'program-build', cwd=execution, echo=False):
        raise RuntimeError('Bare-metal program compilation failed')
    subprocess.run([objcopy, '-O', 'binary', *(['-j', '.boot'] if dram_text else []),
                    str(execution / 'program.elf'), str(execution / 'program.bin')], check=True)
    if dram_text:
        from elf_image import stage
        stage(execution / 'program.elf', execution)
    disassembly = subprocess.check_output(['riscv64-unknown-elf-objdump', '-d', str(execution / 'program.elf')], text=True)
    (execution / 'program.dis').write_text(disassembly, encoding='utf-8')
    metadata = read(root / 'scenario/ports.json')
    write(execution / 'startup.json', {'initial_inputs': {p['name']: 0 for p in metadata['ports'] if p['direction'] == 'input'},
          'settle_evaluations': 1, 'main_reset_cycles': 32, 'clock_samples_per_cycle': [0,0,1,0],
          'assertions_enabled': True, 'internal_cpu_state_assigned': False,
          'unknown_initial_choice': 'Verilator default zero experiment; known Lean sampler constraints are retained separately.'})
    write(execution / 'environment.json', {'irq_on_read_wait': irq_on_read_wait,
          'external_irq_bits': 1 if irq_on_read_wait else 0,
          'delayed_read_address': 0x50000000 if irq_on_read_wait else None,
          'extra_read_latency_cycles': 500 if irq_on_read_wait else 0,
          'test_device_initial_value': 0x12345678 if irq_on_read_wait else None})
    (execution / 'program.cpp').write_text(harness(metadata, cycles, observe, dram_text, irq_on_read_wait), encoding='utf-8')
    if observe:
        write(execution / 'observations.json', [{k: p[k] for k in ('name','wire','width')} for p in wire_observations()])
    obj = rtl / 'obj_dir'
    library = obj / 'VXSTop__ALL.a'
    verilator = Path(os.environ['VERILATOR_ROOT'])
    dependencies = [library, obj / 'verilated.o', obj / 'verilated_threads.o', obj / 'VXSTop.h', obj / 'VXSTop___024root.h',
                    obj / 'VXSTop_DataModule_CtrlPcMem_16entry.h']
    hashes = {str(p): digest(p) for p in dependencies}
    image_files = [execution / 'program.bin', execution / 'program.elf']
    if dram_text:
        image_files += [execution / 'elf-image.json', execution / 'segments.csv', *execution.glob('segment-*.bin')]
    image_hashes = {path.name: digest(path) for path in image_files}
    write(execution / 'provenance.json', {'bundle_manifest_sha256': digest(root / MANIFEST), 'host': host,
          'original_hardware_modified': False, 'cached_rtl_dependencies_sha256': hashes,
          'program_sha256': digest(execution / 'program.bin'), 'cycles_limit': cycles,
          'program_elf_sha256': digest(execution / 'program.elf'), 'dram_text': dram_text,
          'input_image_sha256': image_hashes,
          'testbench_sha256': digest(execution / 'program.cpp'),
          'axi_adapter_sha256': digest(execution / 'axi_memory.h'),
          'program_source_sha256': digest(execution / 'smoke.S'),
          'program_source': str(chosen),
          'linker_script_sha256': digest(execution / 'link.ld'),
          'gcc_sha256': digest(Path(gcc)),
          'gcc_version': subprocess.check_output([gcc, '--version'], text=True).splitlines()[0],
          'read_only_observations': observe,
          'startup_sha256': digest(execution / 'startup.json'),
          'environment_sha256': digest(execution / 'environment.json'),
          'scope': 'Original RTL bring-up only; Lean and independent ISA acceptance remain separate.'})
    command = ['g++', '-std=c++17', '-O2', '-DVL_TIME_CONTEXT', '-I'+str(obj),
               '-I'+str(verilator / 'include'), '-I'+str(verilator / 'include/vltstd'),
               'program.cpp', str(obj / 'verilated.o'), str(obj / 'verilated_threads.o'), str(library),
               '-pthread', '-latomic', '-o', 'run-program']
    if run_logged(command, execution / 'driver-build', cwd=execution, echo=False):
        raise RuntimeError('Program driver compilation failed: ' + str(execution))
    write(execution / 'driver-executable.json', {'source_sha256':digest(execution / 'program.cpp'),
          'executable_sha256':digest(execution / 'run-program'), 'original_rtl_library_sha256':hashes[str(library)]})
    code = run_logged([str(execution / 'run-program')], execution / 'rtl-execution', cwd=execution, echo=False)
    for path, expected in hashes.items():
        if digest(Path(path)) != expected:
            raise ValueError('Original cached RTL dependency changed: ' + path)
    for name, expected in image_hashes.items():
        if digest(execution / name) != expected:
            raise ValueError('Loaded program image changed during execution: ' + name)
    for name, expected in original_sources.items():
        if digest(rtl / name) != expected:
            raise ValueError('Original working RTL changed during program execution: ' + name)
    write(execution / 'result.json', {'status': 'RTL_PROGRAM_COMPLETED' if code == 0 else 'RTL_FAILED',
          'exit_code': code, 'execution': str(execution), 'lean_comparison': 'NOT_RUN', 'isa_comparison': 'NOT_RUN'})
    if code:
        raise RuntimeError('Original RTL program execution failed: ' + str(execution))
    print((execution / 'execution.json').read_text(), flush=True)
    return execution


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--cycles', type=int, default=10000)
    parser.add_argument('--observe', action='store_true', help='Record read-only ROB/FTQ/PC-storage signals for Lean retirement comparison')
    parser.add_argument('--program', type=Path, help='RISC-V assembly source; defaults to the bundled smoke program')
    parser.add_argument('--dram-text', action='store_true', help='Load an ELF with a reset-ROM stub and code in DRAM, exercising instruction-cache fetch')
    parser.add_argument('--irq-on-read-wait', action='store_true', help='Delay test-device read 500 cycles and assert external interrupt while it waits')
    args = parser.parse_args()
    if args.cycles < 33:
        parser.error('Allow reset and at least one active cycle')
    run(args.bundle.resolve(), args.work.resolve(), args.cycles, args.observe, args.program, args.dram_text, args.irq_on_read_wait)


if __name__ == '__main__':
    main()
