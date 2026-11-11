"""
Lean 代码检查器 - 使用 lean 工具检查生成的 Lean 4 代码是否可编译。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LeanCheckResult:
    """Lean 编译检查结果"""
    success: bool
    stdout: str
    stderr: str
    checked_files: list[str] = field(default_factory=list)
    lean_exe: str = "lean"
    cmd: str = ""


def _run_lean_on_file(entry_file: Path, lean_exe: str = "lean", env: dict | None = None) -> LeanCheckResult:
    if not entry_file.exists():
        raise FileNotFoundError(f"Lean entry file not found: {entry_file}")

    cmd = [lean_exe, str(entry_file)]
    env = env or os.environ.copy()
    # Prepend tmpdir to LEAN_PATH instead of overriding
    existing_path = env.get("LEAN_PATH", "")
    if existing_path:
        env["LEAN_PATH"] = f"{entry_file.parent}:{existing_path}"
    else:
        env["LEAN_PATH"] = str(entry_file.parent)

    source = entry_file.read_text(encoding="utf-8")
    if re.search(r"\b(?:sorry|admit)\b|\baxiom\b", source):
        return LeanCheckResult(
            success=False, stdout="", stderr="forbidden unchecked declaration",
            checked_files=[str(entry_file)], lean_exe=lean_exe,
            cmd=f"{lean_exe} {entry_file}",
        )
    completed = subprocess.run(
        cmd,
        cwd=entry_file.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    success = completed.returncode == 0

    return LeanCheckResult(
        success=success,
        stdout=completed.stdout,
        stderr=completed.stderr,
        checked_files=[str(entry_file)],
        lean_exe=lean_exe,
        cmd=" ".join(cmd),
    )


def check_lean_files(entry_file: Path | str, lean_exe: str = "lean") -> LeanCheckResult:
    """检查已存在的 Lean 文件是否可编译。"""
    entry = Path(entry_file)
    return _run_lean_on_file(entry, lean_exe=lean_exe)


def _combine_lean_and_verification(
    module_name: str,
    lean_code: str,
    verification_code: str,
) -> str:
    """Combine generated Lean code and verification code into a single file.

    This avoids Lean import lookup issues in temporary directories when the
    verification file imports the generated module.
    """
    main_lines = lean_code.splitlines()
    first_import = next(
        (idx for idx, line in enumerate(main_lines) if line.strip().startswith("import")),
        len(main_lines),
    )
    prefix_lines = main_lines[:first_import]
    main_imports = [line for line in main_lines[first_import:] if line.strip().startswith("import")]
    main_body = [line for line in main_lines[first_import:] if not line.strip().startswith("import")]

    extra_imports: list[str] = []
    verification_body = []
    for line in verification_code.splitlines():
        stripped = line.strip()
        if not stripped:
            verification_body.append(line)
            continue
        if stripped.startswith("import "):
            if stripped == f"import {module_name}":
                continue
            if stripped not in main_imports and stripped not in extra_imports:
                extra_imports.append(stripped)
            continue
        verification_body.append(line)

    combined_lines = []
    combined_lines.extend(prefix_lines)
    combined_lines.extend(main_imports)
    combined_lines.extend(extra_imports)
    combined_lines.extend(main_body)
    combined_lines.append("")
    combined_lines.extend(verification_body)

    return "\n".join(combined_lines).strip() + "\n"


def check_lean_code(
    module_name: str,
    lean_code: str,
    verification_code: str | None = None,
    lean_exe: str = "lean",
) -> LeanCheckResult:
    """将生成的 Lean 代码写入临时文件并执行 lean 编译检查。

    If verification_code is provided, both the generated module and verification
    code are combined into a single file to avoid Lean import lookup issues.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # 如果有验证代码，合并为主文件
        if verification_code is not None:
            combined_code = _combine_lean_and_verification(module_name, lean_code, verification_code)
            main_file = tmp_path / f"{module_name}.lean"
            main_file.write_text(combined_code, encoding="utf-8")
            return _run_lean_on_file(main_file, lean_exe=lean_exe)

        # 否则只检查主文件
        main_file = tmp_path / f"{module_name}.lean"
        main_file.write_text(lean_code, encoding="utf-8")
        return _run_lean_on_file(main_file, lean_exe=lean_exe)
