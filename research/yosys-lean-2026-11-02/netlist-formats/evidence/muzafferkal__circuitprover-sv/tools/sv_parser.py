#!/usr/bin/env python3
"""
sv_parser.py — Extract parameterized SystemVerilog structure (pre-elaboration).

Parses:
  - Parameter declarations (with constraints)
  - Port definitions (with width expressions)
  - State variables (registers)
  - Always blocks (for later translation to step functions)

No full elaboration; keeps parameter expressions symbolic.
"""

import re
import json
import sys
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional, Any


@dataclass
class Parameter:
    """Represents a module parameter."""
    name: str
    type_: str  # "int", "bit", "logic", etc.
    width: Optional[str] = None  # e.g., "8" or None
    default: Optional[str] = None  # e.g., "8" or "'0"
    constraint: Optional[str] = None  # e.g., "> 0" parsed from comments

@dataclass
class Port:
    """Represents a module port."""
    name: str
    direction: str  # "input", "output", "inout"
    type_: str  # "logic", "bit", "reg", etc.
    width_expr: str  # e.g., "[WIDTH-1:0]" or "[7:0]"

@dataclass
class StateVar:
    """Represents a register or wire that holds state."""
    name: str
    type_: str  # "logic", "reg", etc.
    width_expr: str
    init_value: Optional[str] = None  # e.g., "'0"

@dataclass
class AlwaysBlock:
    """Represents an always or always_ff block."""
    block_type: str  # "always_ff", "always_comb", "assign"
    trigger: Optional[str]  # e.g., "@(posedge clk)" or None for always_comb
    body: str  # Raw block body (to be parsed by LLM or more detailed translator)

@dataclass
class ModuleInfo:
    """Extracted module information."""
    name: str
    parameters: List[Parameter]
    ports: List[Port]
    state_vars: List[StateVar]
    always_blocks: List[AlwaysBlock]
    requires: List[str]  # parameter constraints from `// require: <expr>` comments


def extract_width_expr(width_str: str) -> str:
    """Extract width expression from [WIDTH-1:0] or similar."""
    if not width_str:
        return "1"
    m = re.search(r'\[([^\]]+)\]', width_str)
    if m:
        range_expr = m.group(1)
        # Parse "WIDTH-1:0" -> return "WIDTH"
        parts = range_expr.split(':')
        if len(parts) == 2:
            high = parts[0].strip()
            # Remove trailing "-1" if present
            if high.endswith('-1'):
                return high[:-2]
            return high
    return "1"


def parse_systemverilog(sv_text: str) -> ModuleInfo:
    """Parse a SystemVerilog module definition."""

    # Extract module name and parameter list
    module_match = re.search(
        r'module\s+(\w+)\s*(?:#\((.*?)\))?\s*\((.*?)\);',
        sv_text,
        re.DOTALL
    )
    if not module_match:
        raise ValueError("No module definition found")

    module_name = module_match.group(1)
    params_str = module_match.group(2) or ""
    ports_str = module_match.group(3) or ""

    # Parse parameters
    parameters = parse_parameters(params_str)

    # Parse ports
    ports = parse_ports(ports_str)

    # Parse state variables (logic/reg declarations inside module)
    # Extract everything after port list until the first always/assign
    state_section = sv_text[module_match.end():]
    state_section = re.split(r'(always|assign)', state_section)[0]

    state_vars = parse_state_vars(state_section)

    # Parse always blocks and assigns
    always_blocks = parse_always_blocks(sv_text)

    requires = [r.strip() for r in re.findall(r'//\s*require:\s*(.+)', sv_text)]

    return ModuleInfo(
        name=module_name,
        parameters=parameters,
        ports=ports,
        state_vars=state_vars,
        always_blocks=always_blocks,
        requires=requires,
    )


def parse_parameters(params_str: str) -> List[Parameter]:
    """Parse parameter list."""
    params = []
    if not params_str.strip():
        return params

    # Split by comma, but be careful of nested parens
    param_decls = re.findall(
        r'parameter\s+(\w+)\s+(\w+)\s*(?:=\s*([^,;]+))?',
        params_str
    )

    for type_, name, default in param_decls:
        param = Parameter(
            name=name,
            type_=type_,
            default=default.strip() if default else None,
            constraint=None  # Would parse from comments in a full impl
        )
        params.append(param)

    return params


def parse_ports(ports_str: str) -> List[Port]:
    """Parse port list."""
    ports = []
    if not ports_str.strip():
        return ports

    # Split port declarations
    port_decls = re.findall(
        r'(input|output|inout)\s+(\w+)\s+(?:(\[[^\]]+\]))?\s*(\w+)',
        ports_str
    )

    for direction, type_, width_expr, name in port_decls:
        port = Port(
            name=name,
            direction=direction,
            type_=type_,
            width_expr=width_expr or ""
        )
        ports.append(port)

    return ports


def parse_state_vars(state_section: str) -> List[StateVar]:
    """Parse state variable declarations (logic/reg)."""
    vars_ = []

    # Find declarations like: logic [WIDTH-1:0] cnt;
    decls = re.findall(
        r'(logic|reg|wire)\s+(?:(\[[^\]]+\]))?\s*(\w+)\s*(?:=\s*([^;]+))?;',
        state_section
    )

    for type_, width_expr, name, init in decls:
        var = StateVar(
            name=name,
            type_=type_,
            width_expr=width_expr or "1",
            init_value=init.strip() if init else None
        )
        vars_.append(var)

    return vars_


def _split_top_level(sv_text: str) -> List[str]:
    """Split module body into top-level constructs.

    A construct starts at an `always*`/`assign` keyword and runs until the next
    such keyword or `endmodule`. This handles always blocks with or without
    begin/end, which a begin.*?end regex cannot.
    """
    starts = [m.start() for m in re.finditer(
        r'^\s*(always_ff|always_comb|always_latch|always|assign)\b', sv_text, re.MULTILINE)]
    end_idx = sv_text.find('endmodule')
    if end_idx == -1:
        end_idx = len(sv_text)
    chunks = []
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else end_idx
        chunks.append(sv_text[s:e].strip())
    return chunks


def parse_always_blocks(sv_text: str) -> List[AlwaysBlock]:
    """Parse always_ff, always_comb, and assign blocks."""
    blocks = []

    for chunk in _split_top_level(sv_text):
        m = re.match(r'always_ff\s*(@\([^)]*\))\s*(.*)', chunk, re.DOTALL)
        if m:
            blocks.append(AlwaysBlock(block_type="always_ff", trigger=m.group(1), body=m.group(2).strip()))
            continue
        m = re.match(r'always_comb\s*(.*)', chunk, re.DOTALL)
        if m:
            blocks.append(AlwaysBlock(block_type="always_comb", trigger=None, body=m.group(1).strip()))
            continue
        m = re.match(r'always\s*(@\([^)]*\))\s*(.*)', chunk, re.DOTALL)
        if m:
            trig = m.group(1)
            kind = "always_ff" if "posedge" in trig or "negedge" in trig else "always_comb"
            blocks.append(AlwaysBlock(block_type=kind, trigger=trig, body=m.group(2).strip()))
            continue

        m = re.match(r'assign\s+(.+?)\s*=\s*(.+?);', chunk, re.DOTALL)
        if m:
            blocks.append(AlwaysBlock(block_type="assign", trigger=None,
                                      body=f"{m.group(1).strip()} = {m.group(2).strip()};"))

    return blocks


def main():
    if len(sys.argv) < 2:
        print("Usage: sv_parser.py <design.sv>")
        sys.exit(1)

    with open(sys.argv[1], 'r') as f:
        sv_text = f.read()

    try:
        module_info = parse_systemverilog(sv_text)

        # Output as JSON
        output = {
            "module": module_info.name,
            "parameters": [asdict(p) for p in module_info.parameters],
            "ports": [asdict(p) for p in module_info.ports],
            "state_vars": [asdict(s) for s in module_info.state_vars],
            "always_blocks": [asdict(b) for b in module_info.always_blocks],
            "requires": module_info.requires,
        }

        print(json.dumps(output, indent=2))

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
