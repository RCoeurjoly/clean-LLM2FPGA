#!/usr/bin/env python3
"""Verify the committed Task 4 board observations without accessing hardware."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys


ARCHIVED_BITSTREAM_SHA256 = "bcf27e4da6abb290ad96a9d45618b0a3ef207fd5e35efbb2550996741c62426f"
FRAMES_SHA256 = "c78a1ab40fea57c214194b95d989fa5ba5e4a32dbef1ea7a5a1595bde6931617"
DECODED_BITS_SHA256 = "2c3b88fc99f69e1914388e14ec3bcb5fcd5359e9c2d35c7e9ea81b88fe31cceb"
RESTORED_BASELINE_SHA256 = "2e28abb6638b407fd55b681018275c712afe47fde2de24ba8d1ebe584208b91a"
PROGRAMMING_LOG_SHA256 = "ff980b973374db544136b9e400e32ec4bfd2c70387f9039e060bc3b6b88966b6"
STATUS = 0x3000000000000000
START = 0x2000000000000000
RESET = 0x5000000000000000


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def canonical_sha256(document: dict) -> str:
    payload = {key: value for key, value in document.items() if key != "artifact_sha256"}
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(data).hexdigest()


def verify(archive: Path, oracle_path: Path, configuration_path: Path) -> dict:
    observed = json.loads((archive / "hardware/receipt.json").read_text())
    oracle = json.loads(oracle_path.read_text())
    archived_oracle = json.loads((archive / "oracle.json").read_text())
    configuration = json.loads(configuration_path.read_text())
    require(archived_oracle == oracle, "archived oracle differs from committed oracle")
    require(canonical_sha256(oracle) == oracle["artifact_sha256"] == observed["oracle_sha256"],
            "oracle hash mismatch")
    require(configuration["status"] == "source-rebuilt-accepted-task6-configuration", "configuration status")
    require(configuration["archived_bitstream_sha256"] == ARCHIVED_BITSTREAM_SHA256,
            "archived bitstream binding mismatch")
    require(configuration["frames_sha256"] == FRAMES_SHA256 and
            configuration["decoded_bits_sha256"] == DECODED_BITS_SHA256,
            "physical configuration mismatch")
    require(observed["status"] == "passed" and not observed.get("candidate_error") and
            not observed.get("restoration_error"), "recorded board run did not pass")
    require(observed["candidate_sha256"] == ARCHIVED_BITSTREAM_SHA256,
            "programmed bitstream identity mismatch")
    require(observed["claims"] == {"bpi_changed": False, "full_timing_qualification": False},
            "board evidence overclaims flash or timing")
    prompt = oracle["token_contract"]["initial_context"]
    tokens = [step["selected_token"]["value"] for step in oracle["steps"]]
    require(prompt == [7454, 2402, 257, 640] and len(tokens) == 16,
            "unexpected fixed input or reference length")
    require(len(observed["runs"]) == 3, "expected three independent inference runs")
    writes = [0x1000000000000000 | (index << 16) | value for index, value in enumerate(prompt)]
    reads = [0x6000000000000000 | (index << 16) for index in range(4)]
    token_reads = [0x4000000000000000 | index for index in range(16)]
    allowed = set(writes + reads + token_reads + [RESET, START, STATUS])
    elapsed = []
    raw_completion_statuses = []
    for run_index, run in enumerate(observed["runs"]):
        require(run["status"] == "passed" and run["inference_started"], f"run{run_index} status")
        require(run["tokens"] == tokens, f"run{run_index} token summary mismatch")
        require(0 < run["elapsed_seconds"] <= 300, f"run{run_index} exceeds 300 seconds")
        transactions = run["transactions"]
        commands = [int(item["command"], 16) for item in transactions]
        require(commands and commands[0] == RESET, f"run{run_index} missing reset")
        require(all(command in allowed for command in commands), f"run{run_index} unexpected host input")
        require([command for command in commands if command >> 60 == 1] == writes,
                f"run{run_index} prompt writes differ")
        require([command for command in commands if command >> 60 == 6] == reads,
                f"run{run_index} prompt readback differs")
        require([command for command in commands if command >> 60 == 4] == token_reads,
                f"run{run_index} token read sequence differs")
        require(commands.count(RESET) == 1 and commands.count(START) == 1,
                f"run{run_index} reset/START count differs")
        start_index = commands.index(START)
        require(all(commands.index(command) < start_index for command in writes + reads),
                f"run{run_index} prompt setup happened after START")
        # Capture precedes Update-DR: transaction i carries the response to
        # command i-1. A completion edge may clear response_word to zero,
        # so zero captures are transient observations, never proof of done.
        completed = False
        initialized_before_start = False
        completion_status = None
        for index in range(1, len(commands)):
            previous = commands[index - 1]
            actual = int(transactions[index]["captured"], 16)
            require(0 <= actual < (1 << 64), f"run{run_index} invalid raw response")
            if previous == START:
                require(actual in (0, 0x1000000000000000),
                        f"run{run_index} raw START response mismatch")
            if previous == STATUS and actual != 0:
                # Only init/done/busy and prompt[0] are exposed in this ABI.
                flags = actual & ~0xffff
                require(flags in (0x2800000000000000, 0x2a00000000000000,
                                  0x2c00000000000000),
                        f"run{run_index} raw status flags mismatch")
                expected_prompt = prompt[0] if commands.index(writes[0]) < index else 0
                require((actual & 0xffff) == expected_prompt,
                        f"run{run_index} raw status prompt mismatch")
                if index <= start_index:
                    require(flags == 0x2800000000000000,
                            f"run{run_index} completion/busy status before START")
                    initialized_before_start = True
                elif flags == 0x2c00000000000000:
                    completed = True
                    completion_status = actual
                else:
                    require(not completed, f"run{run_index} completion status regressed")
            if commands[index] in token_reads:
                require(completed, f"run{run_index} token read before raw completion status")
        require(initialized_before_start, f"run{run_index} no raw initialized status before START")
        require(completed, f"run{run_index} no raw completion status")
        require(run["last_status"] == f"{completion_status:016x}",
                f"run{run_index} completion status summary differs from raw response")
        raw_completion_statuses.append(f"{completion_status:016x}")
        captured_tokens = []
        for index, command in enumerate(commands):
            if command in writes or command in reads or command in token_reads:
                require(index + 1 < len(commands) and commands[index + 1] == STATUS,
                        f"run{run_index} missing response capture")
                actual = int(transactions[index + 1]["captured"], 16)
                if command in token_reads:
                    require(index > start_index, f"run{run_index} token read before START")
                    captured_tokens.append(actual)
                else:
                    expected = prompt[writes.index(command) if command in writes else reads.index(command)]
                    require(actual == expected, f"run{run_index} prompt read/write echo mismatch")
        require(captured_tokens == tokens, f"run{run_index} raw token capture mismatch")
        require(run["last_status"] == f"{0x2c00000000000000 | prompt[0]:016x}",
                f"run{run_index} completion status mismatch")
        elapsed.append(run["elapsed_seconds"])
    events = observed["programming"]
    require([event["label"] for event in events] ==
            ["run0", "run1", "run2", "restored-first-token"], "programming sequence mismatch")
    for event in events:
        command = event["command"]
        require(event["returncode"] == 0 and 0 < event["elapsed_seconds"] <= 360,
                f"{event['label']} programming failed")
        require(command[1:8] == ["-c", "digilent_hs3", "--ftdi-serial", "210299BF3824",
                                 "--freq", "1000000", "-m"] and len(command) == 9,
                f"{event['label']} programming command mismatch")
        expected_hash = (ARCHIVED_BITSTREAM_SHA256 if event["label"].startswith("run")
                         else RESTORED_BASELINE_SHA256)
        require(event["image_sha256"] == expected_hash, f"{event['label']} programmed image mismatch")
        log_path = archive / "hardware" / f"{event['label']}-program.log"
        require(hashlib.sha256(log_path.read_bytes()).hexdigest() == PROGRAMMING_LOG_SHA256,
                f"{event['label']} programming log hash mismatch")
        log = log_path.read_text()
        require("write to ram" in log and
                re.search(r"isc_done\s+1\s+isc_ena\s+0\s+init\s+1\s+done\s+1", log) is not None,
                f"{event['label']} programming log lacks completion")
    require(observed["baseline_restoration"]["status"] == "passed" and
            observed["baseline_restoration"]["token"] == 11, "first-token baseline was not restored")
    return {
        "status": "passed", "tokens": tokens, "elapsed_seconds": elapsed,
        "independently_programmed_sram_runs": 3,
        "raw_completion_statuses": raw_completion_statuses,
        "archived_bitstream_sha256": ARCHIVED_BITSTREAM_SHA256,
        "source_rebuilt_frames_sha256": FRAMES_SHA256,
        "board_receipt_sha256": hashlib.sha256((archive / "hardware/receipt.json").read_bytes()).hexdigest(),
        "limitations": ["Historical board observations reverified offline; no new board run.",
                        "Independent SRAM reconfiguration, not chassis power cycles.",
                        "BRAM/DSP internal timing coverage is incomplete.",
                        "Only the frozen four-token prompt and 16-token continuation were exercised."],
    }


def main() -> None:
    archive, oracle, configuration, output = map(Path, sys.argv[1:])
    result = verify(archive, oracle, configuration)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
