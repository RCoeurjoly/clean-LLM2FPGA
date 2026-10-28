"""The Task 4 evidence checker must validate observed JTAG data, not a passed flag."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "artifacts/task4/full16-board-2026-09-29"
ORACLE = ROOT / "artifacts/reference/tinystories-1m-fixed-point-continuation-oracle.json"
CHECKER = ROOT / "scripts/pipeline/verify_task4_board_archive.py"


class Task4BoardArchiveTest(unittest.TestCase):
    def run_checker(self, archive: Path, directory: Path, configuration_changes=None) -> subprocess.CompletedProcess[str]:
        configuration = directory / "configuration.json"
        document = {
            "status": "source-rebuilt-accepted-task6-configuration",
            "archived_bitstream_sha256": "bcf27e4da6abb290ad96a9d45618b0a3ef207fd5e35efbb2550996741c62426f",
            "frames_sha256": "c78a1ab40fea57c214194b95d989fa5ba5e4a32dbef1ea7a5a1595bde6931617",
            "decoded_bits_sha256": "2c3b88fc99f69e1914388e14ec3bcb5fcd5359e9c2d35c7e9ea81b88fe31cceb",
        }
        document.update(configuration_changes or {})
        configuration.write_text(json.dumps(document))
        return subprocess.run(
            [sys.executable, "-B", str(CHECKER), str(archive), str(ORACLE),
             str(configuration), str(directory / "verification.json")],
            cwd=ROOT, capture_output=True, text=True,
        )

    def test_authentic_archived_transactions_and_programming_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = self.run_checker(ARCHIVE, directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            verified = json.loads((directory / "verification.json").read_text())
            self.assertEqual(verified["tokens"], [
                11, 612, 373, 257, 1310, 2576, 3706, 20037,
                13, 1375, 6151, 284, 711, 2354, 287, 262,
            ])
            self.assertEqual(verified["independently_programmed_sram_runs"], 3)

    def test_changed_physical_configuration_cannot_reuse_archived_acceptance(self) -> None:
        # Authentic archived transactions must not bless a different design.
        for field in ('archived_bitstream_sha256', 'frames_sha256',
                      'decoded_bits_sha256'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                result = self.run_checker(ARCHIVE, directory, {field: '0' * 64})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((directory / 'verification.json').exists())
                self.assertIn('mismatch', result.stderr.lower())

    def test_altered_raw_token_capture_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            altered = directory / "altered"
            shutil.copytree(ARCHIVE, altered)
            receipt_path = altered / "hardware/receipt.json"
            receipt = json.loads(receipt_path.read_text())
            transactions = receipt["runs"][0]["transactions"]
            first_read = next(index for index, item in enumerate(transactions)
                              if int(item["command"], 16) == 0x4000000000000000)
            transactions[first_read + 1]["captured"] = "000000000000000c"
            receipt_path.chmod(0o644)
            receipt_path.write_text(json.dumps(receipt))
            result = self.run_checker(altered, directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("token", result.stderr.lower())

    def altered_receipt(self, directory: Path, mutate) -> Path:
        altered = directory / "altered"
        shutil.copytree(ARCHIVE, altered)
        path = altered / "hardware/receipt.json"
        receipt = json.loads(path.read_text())
        mutate(receipt["runs"][0])
        path.chmod(0o644)
        path.write_text(json.dumps(receipt))
        return altered

    def test_zeroed_status_polls_are_rejected(self) -> None:
        def mutate(run):
            transactions = run["transactions"]
            for index in range(1, len(transactions)):
                if int(transactions[index - 1]["command"], 16) in (
                    0x3000000000000000, 0x2000000000000000
                ):
                    transactions[index]["captured"] = "0000000000000000"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = self.run_checker(self.altered_receipt(directory, mutate), directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("status", result.stderr.lower())

    def test_token_reads_without_raw_completion_are_rejected(self) -> None:
        def mutate(run):
            for item in run["transactions"]:
                if item["captured"].startswith("2c"):
                    item["captured"] = "2a00000000001d1e"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = self.run_checker(self.altered_receipt(directory, mutate), directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("completion", result.stderr.lower())

    def test_busy_sampling_is_not_required_for_fast_completion(self) -> None:
        def mutate(run):
            for item in run["transactions"]:
                if item["captured"].startswith("2a"):
                    item["captured"] = "2c00000000001d1e"
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            result = self.run_checker(self.altered_receipt(directory, mutate), directory)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_truncated_last_token_capture_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            altered = self.altered_receipt(directory, lambda run: run["transactions"].pop())
            result = self.run_checker(altered, directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("response capture", result.stderr.lower())

    def test_completion_summary_must_match_raw_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            altered = self.altered_receipt(directory, lambda run: run.update(last_status="2800000000001d1e"))
            result = self.run_checker(altered, directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("status", result.stderr.lower())

    def test_programmer_log_changed_without_losing_success_markers_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            altered = directory / "altered"
            shutil.copytree(ARCHIVE, altered)
            log = altered / "hardware/run1-program.log"
            log.chmod(0o644)
            log.write_text(log.read_text() + "\n")
            result = self.run_checker(altered, directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("programming log hash", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
