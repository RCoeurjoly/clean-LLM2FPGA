"""Project-local tool discovery and logged subprocess execution."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
LEAN_VERSION = "4.19.0"
SBT_VERSION = "1.8.2"
CHISEL_VERSION = "7.3.0"
SCALA_VERSION = "2.13.15"
FIRTOOL_VERSION = "1.135.0"


def rtl_environment():
    env = os.environ.copy()
    platform = "windows" if os.name == "nt" else "linux"
    directory = ROOT / ".tools" / f"firtool-{FIRTOOL_VERSION}" / f"firtool-{FIRTOOL_VERSION}" / "bin"
    if directory.is_dir():
        env["CHISEL_FIRTOOL_PATH"] = str(directory)
    return env


def java_executable() -> str:
    configured = os.environ.get("CHISEL2LEAN_JAVA")
    if configured:
        return configured
    suffix = "java.exe" if os.name == "nt" else "java"
    homes = [os.environ.get("JAVA_HOME"), str(ROOT.parent / ".tools" / "jdk-17.0.20.1+1")]
    for home in homes:
        if home and (Path(home) / "bin" / suffix).is_file():
            return str(Path(home) / "bin" / suffix)
    found = shutil.which("java")
    if found:
        return found
    raise RuntimeError("Java 17 not found; set JAVA_HOME or CHISEL2LEAN_JAVA")


def lean_executable() -> str:
    configured = os.environ.get('CHISEL2LEAN_LEAN')
    if configured:
        executable = Path(configured).expanduser().resolve()
        if not executable.is_file():
            raise RuntimeError('CHISEL2LEAN_LEAN must name an installed Lean executable')
        return str(executable)
    suffix = "lean.exe" if os.name == "nt" else "lean"
    platform = "windows" if os.name == "nt" else "linux"
    path = ROOT / ".tools" / f"lean-{LEAN_VERSION}-{platform}" / "bin" / suffix
    if path.is_file():
        return str(path)
    raise RuntimeError("Pinned Lean not installed; run python scripts/bootstrap.py --lean")


def lean_runtime_files(executable: str | None = None) -> dict[str, Path]:
    """Record host binaries and shared libraries relative to the Lean bin dir."""
    lean = Path(executable or lean_executable()).resolve()
    directory = lean.parent
    suffix = '.exe' if os.name == 'nt' else ''
    files = {lean, *(directory / (name + suffix) for name in ('leanc', 'clang', 'ld.lld'))}
    files.update(directory.glob('*.dll'))
    for library_directory in (directory.parent / 'lib', directory.parent / 'lib/lean'):
        for pattern in ('*.so', '*.so.*', '*.dylib'):
            files.update(library_directory.glob(pattern))
    return {Path(os.path.relpath(path, directory)).as_posix(): path
            for path in sorted(files) if path.is_file()}


def sbt_command(commands: list[str]) -> tuple[list[str], dict[str, str]]:
    launcher = ROOT / ".tools" / f"sbt-launch-{SBT_VERSION}.jar"
    if not launcher.is_file():
        raise RuntimeError("sbt launcher not installed; run python scripts/bootstrap.py --sbt")
    cache = ROOT / ".cache"
    env = os.environ.copy()
    env["COURSIER_CACHE"] = str(cache / "coursier")
    env["COURSIER_REPOSITORIES"] = "https://repo.maven.apache.org/maven2"
    cmd = [java_executable(), "-Xmx2g", "-XX:ActiveProcessorCount=4",
           "-Dfile.encoding=UTF-8", "-Dsbt.supershell=false", "-Dsbt.log.noformat=true",
           "-Dsbt.server.autostart=false", "-Dsbt.override.build.repos=true",
           f"-Dsbt.repository.config={ROOT / 'project' / 'repositories'}",
           f"-Dsbt.boot.directory={cache / 'sbt-boot'}",
           f"-Dsbt.global.base={cache / 'sbt-global'}",
           f"-Dsbt.ivy.home={cache / 'ivy'}", "-jar", str(launcher), *commands]
    return cmd, env


def run_logged(cmd: list[str], log_dir: Path, cwd: Path = ROOT,
               env: dict[str, str] | None = None, echo: bool = True) -> int:
    log_dir.mkdir(parents=True, exist_ok=True)
    metadata = {"command": cmd, "cwd": str(cwd),
                "started_at": dt.datetime.now(dt.timezone.utc).isoformat()}
    (log_dir / "command.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    metadata["pid"] = proc.pid
    (log_dir / "command.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    lock = threading.Lock()

    def copy_stream(stream, name):
        with (log_dir / name).open("w", encoding="utf-8") as destination:
            for line in stream:
                destination.write(line)
                destination.flush()
                if echo:
                    with lock:
                        sys.stdout.write(line)
                        sys.stdout.flush()

    threads = [threading.Thread(target=copy_stream, args=(proc.stdout, "stdout.log")),
               threading.Thread(target=copy_stream, args=(proc.stderr, "stderr.log"))]
    for thread in threads:
        thread.start()
    result = proc.wait()
    for thread in threads:
        thread.join()
    metadata.update(exit_code=result, ended_at=dt.datetime.now(dt.timezone.utc).isoformat())
    (log_dir / "result.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    (log_dir / "exit-status.txt").write_text(str(result) + "\n", encoding="utf-8")
    return result
