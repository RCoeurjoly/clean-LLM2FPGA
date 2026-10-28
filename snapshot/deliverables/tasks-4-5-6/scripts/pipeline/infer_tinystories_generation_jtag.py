#!/usr/bin/env python3
"""Read an already programmed compiler-generated continuation over USER1.

One START is followed by completion polling and indexed token readback. This
checker neither programs the FPGA nor supplies generated tokens to the core.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys
import time


STATUS = 0x3000000000000000


def infer_generation(client, *, clock_tms, reset_tap, shift_ir, prompt,
                     expected_tokens, timeout=300, delay=time.sleep,
                     monotonic=time.monotonic):
    result = {"schema": "tinystories-generation-jtag-v1", "status": "failed",
              "transactions": [], "tokens": [], "inference_started": False,
              "transient_zero_status_reads": 0,
              "independent_reconfiguration_verified": False}
    begun = monotonic()

    def exchange(command):
        clock_tms(client, [1, 0, 0])
        bits = client.shift([0] * 63 + [1], [(command >> i) & 1 for i in range(64)])
        clock_tms(client, [1, 0])
        if len(bits) != 64:
            raise RuntimeError(f"short JTAG response: {len(bits)} bits")
        value = sum((bit & 1) << i for i, bit in enumerate(bits))
        result["transactions"].append({"command": f"{command:016x}",
                                       "captured": f"{value:016x}"})
        delay(0.01)
        return value

    def read(command):
        exchange(command)
        return exchange(STATUS)

    def expect(command, expected):
        actual = read(command)
        if actual != expected:
            raise RuntimeError(f"command {command:016x}: expected {expected:016x}, got {actual:016x}")

    try:
        if len(prompt) != 4 or any(type(x) is not int or not 0 <= x <= 65535 for x in prompt):
            raise ValueError("four unsigned 16-bit prompt words required")
        if len(expected_tokens) not in (2, 16) or any(
                type(x) is not int or not 0 <= x <= 65535 for x in expected_tokens):
            raise ValueError("two or sixteen unsigned 16-bit expected tokens required")
        if not 0 < timeout <= 300:
            raise ValueError("timeout must be in (0, 300]")
        reset_tap(client)
        shift_ir(client, 2, 6)
        delay(0.01)
        expect(0x5000000000000000, 0)
        expect(STATUS, 0x2800000000000000)
        for index, word in enumerate(prompt):
            expect(0x1000000000000000 | (index << 16) | word, word)
        for index, word in enumerate(prompt):
            expect(0x6000000000000000 | (index << 16), word)
        expect(STATUS, 0x2800000000000000 | prompt[0])
        begun = monotonic()
        result["inference_started"] = True
        exchange(0x2000000000000000)
        while monotonic() - begun < timeout:
            status = read(STATUS)
            result["last_status"] = f"{status:016x}"
            if status == 0 and result["transient_zero_status_reads"] == 0:
                result["transient_zero_status_reads"] += 1
                continue
            if status == (0x2c00000000000000 | prompt[0]):
                break
            if status != (0x2a00000000000000 | prompt[0]):
                raise RuntimeError(f"unexpected inference status {status:016x}")
            delay(0.01)
        else:
            raise RuntimeError("inference timeout")
        for index, expected in enumerate(expected_tokens):
            token = read(0x4000000000000000 | index)
            result["tokens"].append(token)
            if token != expected:
                raise RuntimeError(
                    f"token mismatch at index {index}: expected {expected}, got {token}")
            if monotonic() - begun > timeout:
                raise RuntimeError("inference timeout during token readback")
        result["status"] = "passed"
    except Exception as error:
        result["error"] = str(error)
    result["elapsed_seconds"] = monotonic() - begun
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport-dir", type=Path, required=True)
    parser.add_argument("--serial", default="210299BF3824")
    parser.add_argument("--generation-steps", type=int, choices=(2, 16), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from TinyStories.capture_fixed_point_continuation_oracle import verify_continuation_oracle
    oracle = json.loads((root / "artifacts/reference/tinystories-1m-fixed-point-continuation-oracle.json").read_text())
    verify_continuation_oracle(oracle)
    sys.path.insert(0, str(args.transport_dir.resolve()))
    ftdi = importlib.import_module("read_jtag_debug_ftdi_bitbang")
    jtag = importlib.import_module("read_jtag_debug_xvc")
    expected = [step["selected_token"]["value"] for step in oracle["steps"][:args.generation_steps]]
    client = None
    result = {"schema": "tinystories-generation-jtag-v1", "status": "failed"}
    try:
        client = ftdi.FtdiMpsseJtag(serial=args.serial, vid=0x0403, pid=0x6014,
                                   freq_hz=1_000_000, tdo_bit=7)
        result = infer_generation(
            client, clock_tms=jtag.clock_tms, reset_tap=jtag.reset_tap,
            shift_ir=jtag.shift_ir, prompt=oracle["token_contract"]["initial_context"],
            expected_tokens=expected, timeout=args.timeout)
    except Exception as error:
        result["error"] = str(error)
    finally:
        if client is not None:
            client.close()
    result["oracle_sha256"] = oracle["artifact_sha256"]
    result["transport_serial"] = args.serial
    result["transport_sha256"] = {
        Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
        for module in (ftdi, jtag)
    }
    result["checker_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    result["receipt_sha256"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({key: value for key, value in result.items() if key != "transactions"}, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
