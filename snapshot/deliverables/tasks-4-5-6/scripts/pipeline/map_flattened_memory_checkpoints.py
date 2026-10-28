#!/usr/bin/env python3
"""Map a flattened generated top's memories in restartable checkpoints.

Yosys can map the complete flattened TinyStories top, but retaining all
intermediate memory structures in one process can exceed the host's memory.
This helper deliberately starts a fresh bounded process for each memory.  A
checkpoint is written only after that memory has been mapped, so an interrupted
run can resume without repeating completed work.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path


LUTRAM_NAMES = {
    "attention_context_q16_16.mem",
    "attention_residual_q16_16.mem",
    "block_input_q16_16.mem",
    "block_output_q16_16.mem",
    "ln1_output_q16_16.mem",
    "ln2_output_q16_16.mem",
    "final_output_q16_16.mem",
    # These are the production runner's distributed variants.  Mapping one
    # of them with the BRAM-only library rejects its ram_style before the
    # checkpoint chain can reach the large immutable model ROMs.
    "q_input_codes_i8.mem",
    "q_input_q16_16.mem",
    "q_accumulator_i64.mem",
    "q_output_q16_16.mem",
    "k_input_q16_16.mem",
    "k_output_q16_16.mem",
    "v_input_q16_16.mem",
    "v_output_q16_16.mem",
    "out_input_q16_16.mem",
    "out_output_q16_16.mem",
    "c_fc_input_codes_i8.mem",
    "c_fc_input_q16_16.mem",
    "c_fc_output_codes_i8.mem",
    "c_fc_output_q16_16.mem",
    "c_proj_input_q16_16.mem",
    "c_proj_output_q16_16.mem",
    "c_proj_post_weight_rescale_bias_q16_16.mem",
    "attention_score_q8.mem",
    "attention_delta_q8.mem",
    "attention_exp_q1_20.mem",
    "lm_input_q16_16.mem",
}

IMMUTABLE_LUTRAM_NAMES = {
    # The board-evidenced split leaves c_proj distributed: mapping both
    # projection ROMs to BRAM regressed the first token from 11 to 547.
    # c_fc uses the default BRAM descriptor to relieve routing pressure.
    "c_proj_weight_codes_i8.mem",
    "token_weight_scale_q8_24_b0.mem",
    "token_weight_scale_q8_24_b1.mem",
    "token_weight_scale_q8_24_b2.mem",
    "token_weight_scale_q8_24_b3.mem",
}
LUTRAM_NAMES |= IMMUTABLE_LUTRAM_NAMES


def memory_names(il: Path) -> list[str]:
    names = re.findall(
        r"^\s*memory(?: width \d+)?(?: size \d+)? \\([^\s]+)$",
        il.read_text(),
        flags=re.MULTILINE,
    )
    if not names:
        raise ValueError(f"no RTLIL memories found in {il}")
    return names


def checkpoint_script(source: Path, destination: Path, name: str, top: str = "main") -> str:
    use_lutram = any(name.endswith(suffix) for suffix in LUTRAM_NAMES)
    library = (
        "+/xilinx/lutrams_xc5v.txt"
        if use_lutram
        else "+/xilinx/brams_xc4v.txt -D HAS_SIZE_36 -D HAS_CASCADE -D HAS_MIXWIDTH_SDP"
    )
    if any(name.endswith(suffix) for suffix in IMMUTABLE_LUTRAM_NAMES):
        library = str(Path(__file__).with_name("rom_lutrams_xc7.txt").resolve())
    # Yosys uses brams_xc4v.txt with these defines for Series 7, paired with
    # brams_xc6v_map.v. Keep read/write ports and width parameters in the native
    # ABI: a TDP port supports at most 36 bits; 72-bit ports require SDP mode.
    selection = f"{top}/\\{name}"
    # Override only the synthesis hint, not the already verified SV primitive.
    # Its two-cycle address/result staging is retained by memory_libmap.
    style = f'setattr -set ram_style "distributed" {selection}\n' if use_lutram else ""
    return (
        f"read_rtlil {source}\n"
        + style +
        f"memory_libmap -lib {library} {selection}\n"
        f"write_rtlil {destination}\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path, help="flattened, memory_dff-normalized RTLIL")
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--yosys", default="yosys")
    parser.add_argument("--top", default="main")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--limit", type=int, default=0, help="map at most this many new memories")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    names = memory_names(args.input)
    previous = args.input
    completed = 0
    failure: str | None = None
    for index, name in enumerate(names, 1):
        destination = args.output_dir / f"map-{index}.il"
        if destination.exists():
            previous = destination
            completed += 1
            continue
        if args.limit and completed >= args.limit:
            break
        with tempfile.NamedTemporaryFile("w", suffix=".ys", delete=False) as script:
            script.write(checkpoint_script(previous, destination, name, args.top))
            script_path = Path(script.name)
        try:
            try:
                subprocess.run(
                    [args.yosys, "-s", str(script_path)],
                    check=True,
                    timeout=args.timeout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            except subprocess.TimeoutExpired:
                failure = f"timeout mapping memory {index}: {name}"
                break
            except subprocess.CalledProcessError as exc:
                failure = f"yosys failed mapping memory {index}: {name} (exit {exc.returncode})"
                break
        finally:
            script_path.unlink(missing_ok=True)
        previous = destination
        completed += 1

    receipt = {
        "schema": "llm2fpga-flattened-memory-checkpoints-v1",
        "input": str(args.input),
        "memory_count": len(names),
        "completed": completed,
        "last_checkpoint": str(previous),
        "status": "complete" if completed == len(names) else "partial",
    }
    if failure:
        receipt["failure"] = failure
    (args.output_dir / "memory-checkpoints.json").write_text(
        json.dumps(receipt, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
