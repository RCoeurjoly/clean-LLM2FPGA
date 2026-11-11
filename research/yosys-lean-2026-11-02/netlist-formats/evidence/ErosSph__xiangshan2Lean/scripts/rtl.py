"""Independent official-Chisel RTL generation and Verilator trace execution."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
from common import ROOT, java_executable, run_logged, rtl_environment


def native_environment():
    env = os.environ.copy()
    if os.name == "nt":
        mingw = Path("C:/msys64/mingw64/bin")
        msys = Path("C:/msys64/usr/bin")
        if mingw.is_dir() and msys.is_dir():
            env["PATH"] = os.pathsep.join([str(mingw), str(msys), env.get("PATH", "")])
    return env


def verilator_command():
    configured = os.environ.get('CHISEL2LEAN_VERILATOR')
    if configured:
        executable = Path(configured).expanduser().resolve()
        if not executable.is_file():
            raise RuntimeError('CHISEL2LEAN_VERILATOR must name an installed Verilator executable')
        return [str(executable)]
    if os.name == "nt" and Path("C:/msys64/usr/bin/perl.exe").is_file():
        return ["C:/msys64/usr/bin/perl.exe", "/mingw64/bin/verilator"]
    executable = shutil.which("verilator")
    if not executable:
        raise RuntimeError("Verilator not found")
    return [executable]


def get_value(values: dict, path: str):
    if path in values:
        return values[path]
    parts = path.split(".")
    column = ".".join(part for part in parts if not part.isdigit())
    if column in values:
        value = values[column]
        for part in parts:
            if part.isdigit():
                value = value[int(part)]
        return value
    key = path
    indices = []
    while key not in values and "." in key:
        key, index = key.rsplit(".", 1)
        if not index.isdigit():
            raise ValueError(f"Missing input {path}")
        indices.insert(0, int(index))
    value = values[key]
    for index in indices:
        value = value[index]
    return value


def run_rtl(ir_path: Path, parameters: dict, trace: list[dict], evidence: Path) -> tuple[int, list[dict]]:
    ir = json.loads(ir_path.read_text(encoding="utf-8"))
    classpath_file = ROOT / ".cache" / "reference-classpath.txt"
    if not classpath_file.is_file():
        raise RuntimeError("Build reference/writeReferenceClasspath first")
    evidence.mkdir(parents=True, exist_ok=True)
    directory = evidence / "rtl"
    directory.mkdir(exist_ok=True)
    values = []
    for parameter in ir["parameters"]:
        v = parameters[parameter["name"]]
        values.append(str(v).lower() if isinstance(v, bool) else str(v))
    command = [java_executable(), "-Xmx2g", "-cp", classpath_file.read_text(encoding="utf-8").strip(),
               "reference.GenerateRTL", ir["module"], str(directory), *values]
    if code := run_logged(command, evidence / "elaboration", env=rtl_environment(), echo=False):
        return code, []
    metadata = json.loads((directory / "ports.json").read_text(encoding="utf-8"))
    return run_emitted_rtl(directory, metadata, trace, evidence)


def run_emitted_rtl(directory: Path, metadata: dict, trace: list[dict], evidence: Path) -> tuple[int, list[dict]]:
    """Execute an independently emitted RTL circuit with its declared interface."""
    ports = metadata["ports"]
    has_clock = metadata["clocked"] and any(p["name"] == "clock" for p in ports)
    has_reset = metadata["clocked"] and any(p["name"] == "reset" for p in ports)
    implicit = ({"clock"} if has_clock else set()) | ({"reset"} if has_reset else set())
    inputs = [p for p in ports if p["direction"] == "input" and p["name"] not in implicit]
    outputs = [p for p in ports if p["direction"] == "output"]
    physical_inputs = [p for p in ports if p["direction"] == "input"]
    initial_inputs = metadata.get("initial_inputs")
    if initial_inputs is not None:
        if not isinstance(initial_inputs, dict) or set(initial_inputs) != {p["name"] for p in physical_inputs}:
            raise ValueError("initial_inputs must specify every physical input, including clocks and resets")
        for port in physical_inputs:
            value = initial_inputs[port["name"]]
            if type(value) not in (int, bool) or not 0 <= value < (1 << port["width"]):
                raise ValueError("Initial input is outside its unsigned port width: " + port["name"])
    # This is an input condition, not an assignment to internal registers. In
    # particular, a reset initially high must have a real edge after settling.
    initial_poke = "\n  ".join(
        f'readWide("{int(initial_inputs[p["name"]])}", dut.{p["rtl_name"]}.data(), {(p["width"] + 31) // 32});'
        if p["width"] > 64 else f'dut.{p["rtl_name"]} = {int(initial_inputs[p["name"]])}ULL;'
        for p in physical_inputs if p["width"]
    ) if initial_inputs is not None else ""
    initial_check = 'if (Verilated::gotError()) { std::cerr << "RTL assertion failed during initial-input settling\\n"; return 7; }' if metadata.get('assertions') else ''
    pre_check = 'if (Verilated::gotError()) { std::cerr << "RTL assertion failed at sample " << cycle << " (pre)\\n"; return 7; }' if metadata.get('assertions') else ''
    post_check = 'if (Verilated::gotError()) { std::cerr << "RTL assertion failed at sample " << cycle << " (post)\\n"; return 7; }' if metadata.get('assertions') else ''
    falling_check = 'if (Verilated::gotError()) { std::cerr << "RTL assertion failed after sample " << (cycle - 1) << " (falling edge)\\n"; return 7; }' if has_clock and metadata.get('assertions') else ''
    initial_eval = f"""{initial_poke}
  Verilated::timeInc(1);
  dut.eval();
  {initial_check}
""" if initial_inputs is not None else ""
    evidence.mkdir(parents=True, exist_ok=True)
    startup = {
        "mode": "explicit-input-settle" if initial_inputs is not None else "first-trace-row",
        "initial_inputs": initial_inputs,
        "sampled": False,
        "internal_state": "Verilator --x-initial 0; no internal state is assigned by the driver",
        "assertions_enabled": bool(metadata.get("assertions")),
    }
    (evidence / "startup.json").write_text(json.dumps(startup, indent=2), encoding="utf-8")
    with (directory / "trace.csv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.writer(output)
        writer.writerow(["reset", *(p["name"] for p in inputs)])
        for entry in trace:
            values = [int(entry.get("reset", False))]
            for port in inputs:
                value = int(get_value(entry["inputs"], port["name"]))
                values.append(value % (1 << port["width"]))
            writer.writerow(values)
    top = metadata["top"]
    poke = "\n    ".join(
        f"readWide(values[{i + 1}], dut.{p['rtl_name']}.data(), {(p['width'] + 31) // 32});" if p["width"] > 64
        else f"dut.{p['rtl_name']} = std::stoull(values[{i + 1}]);"
        for i, p in enumerate(inputs) if p["width"])
    sample = ", ".join(
        f"wideHex(dut.{p['rtl_name']}.data(), {(p['width'] + 31) // 32})" if p["width"] > 64
        else f"std::to_string(static_cast<uint64_t>(dut.{p['rtl_name']}))" if p["width"] else 'std::string("0")'
        for p in outputs)
    header = ["cycle", *("pre:" + p["name"] for p in outputs), *("post:" + p["name"] for p in outputs)]
    code = f"""#include "V{top}.h"
#include "verilated.h"
#include <array>
#include <cstdint>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>
void readWide(const std::string& decimal, WData* words, size_t count) {{
  for (size_t i = 0; i < count; ++i) words[i] = 0;
  for (char digit : decimal) {{
    uint64_t carry = static_cast<unsigned>(digit - '0');
    for (size_t i = 0; i < count; ++i) {{
      uint64_t value = uint64_t(words[i]) * 10 + carry;
      words[i] = static_cast<WData>(value);
      carry = value >> 32;
    }}
  }}
}}
std::string wideHex(const WData* words, size_t count) {{
  std::ostringstream result;
  result << "0x" << std::hex << std::setfill('0');
  for (size_t i = count; i > 0; --i) result << std::setw(8) << words[i - 1];
  return result.str();
}}
int main(int argc, char** argv) {{
  Verilated::commandArgs(argc, argv);
  {'Verilated::fatalOnError(false);' if metadata.get('assertions') else ''}
  V{top} dut;
  {initial_eval}
  std::ifstream input("trace.csv");
  std::ofstream output("rtl-results.csv");
  output << "{','.join(header)}\\n";
  std::string line;
  std::getline(input, line);
  size_t cycle = 0;
  while (std::getline(input, line)) {{
    std::stringstream row(line);
    std::string token;
    std::array<std::string, {len(inputs) + 1}> values;
    size_t field = 0;
    while (std::getline(row, token, ',')) values.at(field++) = token;
    {'dut.clock = 0;' if has_clock else ''}
    {'dut.reset = std::stoull(values[0]);' if has_reset else ''}
    {poke}
    Verilated::timeInc(1);
    dut.eval();
    {pre_check}
    std::array<std::string, {len(outputs)}> pre = {{{sample}}};
    {'dut.clock = 1;' if has_clock else ''}
    Verilated::timeInc(1);
    dut.eval();
    {post_check}
    std::array<std::string, {len(outputs)}> post = {{{sample}}};
    output << cycle++;
    for (auto value : pre) output << ',' << value;
    for (auto value : post) output << ',' << value;
    output << '\\n';
    {'dut.clock = 0; Verilated::timeInc(1); dut.eval();' if has_clock else ''}
    {falling_check}
  }}
  dut.final();
  return 0;
}}
"""
    (directory / "trace.cpp").write_text(code, encoding="utf-8")
    shutil.copy2(directory / "trace.cpp", evidence / "trace.cpp")
    shutil.copy2(directory / "trace.csv", evidence / "trace.csv")
    shutil.copy2(Path(__file__), evidence / "driver.py")
    (evidence / "driver.json").write_text(json.dumps({
        "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256((directory / "trace.cpp").read_bytes()).hexdigest(),
        "trace_sha256": hashlib.sha256((directory / "trace.csv").read_bytes()).hexdigest(),
    }, indent=2), encoding="utf-8")
    env = native_environment()
    optimization = metadata.get('optimization', 2)
    if optimization not in (0, 1, 2, 3):
        raise ValueError('RTL compiler optimization must be between zero and three')
    jobs = metadata.get('jobs', 2)
    if type(jobs) is not int or jobs < 1:
        raise ValueError('RTL compiler jobs must be a positive integer')
    units = metadata.get('rtl_sources', [top + '.v'])
    source_arguments = units
    if len(units) > 128:
        filelist = directory / 'c2l-reference.f'
        filelist.write_text('\n'.join(json.dumps(str(path).replace('\\', '/')) for path in units) + '\n', encoding='utf-8')
        source_arguments = ['-f', filelist.name]
    command = [*verilator_command(), "--cc", "--exe", "--build", "-j", str(jobs),
               "-Wno-fatal", "--x-initial", "0", "--x-assign", "0",
               "-CFLAGS", f"-O{optimization} -DVL_TIME_CONTEXT", "--top-module", top,
               "--Mdir", "obj_dir", *source_arguments, "trace.cpp"]
    for name, flag in [('output_split', '--output-split'), ('output_split_cfuncs', '--output-split-cfuncs')]:
        if name in metadata:
            if not isinstance(metadata[name], int) or metadata[name] <= 0:
                raise ValueError('RTL output splitting must be a positive integer')
            command.extend([flag, str(metadata[name])])
    if 'output_groups' in metadata:
        groups = metadata['output_groups']
        if type(groups) is not int or groups < 0:
            raise ValueError('RTL output groups must be a nonnegative integer')
        command.extend(['--output-groups', str(groups)])
    command += ['-I' + str(p) for p in metadata.get('rtl_include_dirs', [])]
    if metadata.get('assertions'):
        command.insert(len(verilator_command()), '--assert')
    if result := run_logged(command, evidence / "verilator-build", cwd=directory, env=env, echo=False):
        return result, []
    executable = directory / "obj_dir" / ("V" + top + (".exe" if os.name == "nt" else ""))
    result = run_logged([str(executable)], evidence / "verilator-run", cwd=directory, env=env, echo=False)
    if (directory / "rtl-results.csv").is_file():
        shutil.copy2(directory / "rtl-results.csv", evidence / "rtl-results.csv")
    if result:
        return result, []
    rows = []
    with (directory / "rtl-results.csv").open(encoding="utf-8", newline="") as output:
        for row in csv.DictReader(output):
            item = {"cycle": int(row["cycle"]), "pre": {}, "post": {}}
            for port in outputs:
                for sampling in ("pre", "post"):
                    item[sampling][port["name"]] = int(row[sampling + ":" + port["name"]], 0)
            rows.append(item)
    (evidence / "result.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return 0, rows
