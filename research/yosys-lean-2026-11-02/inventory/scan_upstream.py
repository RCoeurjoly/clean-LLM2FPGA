#!/usr/bin/env python3
"""Reproduce the finite built-in export census from two official Yosys trees.

Usage: python3 scan_upstream.py MAIN_TREE STABLE_TREE
No build or network access. Extracts literal C++ registrations, their help bodies,
and candidate file writers for manual classification. This is evidence collection,
not a C++ parser; the Org inventory records the manually checked interpretation.
"""
import hashlib
import json
from pathlib import Path
import re
import sys

OUT = Path(__file__).resolve().parent
REFS = {
    "main": "63eecf3ddca2f143750f11e37ebd18ecb16b51c7",
    "stable": "28ba3cb926fe376fa05ebfc1b921747d011d4557",
}
REG = re.compile(r'\b(Backend|Frontend|Pass|ScriptPass)\s*\(\s*"([^"\n]+)"\s*,\s*"([^"\n]*)"')
HELP = re.compile(r'\bvoid\s+help\s*\([^)]*\)[^{]*\{')
TOKEN = re.compile(r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[{}]', re.M)

def body_at(text, brace):
    depth = 1
    for token in TOKEN.finditer(text, brace + 1):
        if token.group() == "{":
            depth += 1
        elif token.group() == "}":
            depth -= 1
            if depth == 0:
                return text[brace + 1:token.start()]
    raise ValueError("Unclosed C++ function body")

def cpp_strings(text):
    # Decode C string literals from log() calls; dynamic help is retained in raw C++.
    strings = []
    for call in re.finditer(r'\blog\s*\(\s*((?:"(?:\\.|[^"\\])*"\s*)+)', text):
        chunks = re.findall(r'"(?:\\.|[^"\\])*"', call.group(1))
        def decode(chunk):
            escapes = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "a": "\a", "b": "\b", "v": "\v", "f": "\f", "\n": ""}
            return re.sub(r"\\(.)", lambda m: escapes.get(m[1], "\\" + m[1]), chunk[1:-1], flags=re.S)
        strings.append("".join(decode(x) for x in chunks))
    return "".join(strings)

def scan(root, label):
    regs, writers, manifest = [], [], []
    sources = []
    for prefix in ("backends", "frontends", "passes", "techlibs", "kernel"):
        sources.extend((root / prefix).rglob("*.cc"))
    for file in sorted(sources):
        data = file.read_bytes()
        text = data.decode()
        path = file.relative_to(root).as_posix()
        blob_sha = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        regs_in_file = list(REG.finditer(text))
        for i, reg in enumerate(regs_in_file):
            kind, name, short_help = reg.groups()
            command = name[1:] if name.startswith("=") else ("write_" if kind == "Backend" else "read_" if kind == "Frontend" else "") + name
            end = regs_in_file[i + 1].start() if i + 1 < len(regs_in_file) else len(text)
            hm = HELP.search(text, reg.end(), end)
            help_body = body_at(text, hm.end() - 1) if hm else None
            item = dict(command=command, registry_type=kind, registry_name=name,
                        short_help=short_help, path=path,
                        registration_line=text.count("\n", 0, reg.start()) + 1,
                        blob_sha=blob_sha,
                        url=f"https://github.com/YosysHQ/yosys/blob/{REFS[label]}/{path}",
                        help_line=text.count("\n", 0, hm.start()) + 1 if hm else None,
                        help_literals=cpp_strings(help_body) if help_body else None,
                        help_cpp=help_body)
            regs.append(item)
        if regs_in_file:
            manifest.append(dict(path=path, blob_sha=blob_sha, size=len(data)))
        patterns = (r'\b(?:fopen|freopen)\([^;\n]*"[wa][bt+]*"',
                    r'\b(?:ofstream|fstream)\b', r'write_to_file\(',
                    r'yosys_output_files\.insert', r'log\("[^"\n]*(?:write|dump|export)[^"\n]*(?:file|format)')
        matches = []
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                line = text.count("\n", 0, match.start()) + 1
                matches.append(dict(line=line, text=text.splitlines()[line - 1].strip(), pattern=pattern))
        if matches:
            writers.append(dict(path=path, commands=[x[1][1:] if x[1].startswith("=") else ("write_" if x[0] == "Backend" else "read_" if x[0] == "Frontend" else "") + x[1] for x in (r.groups() for r in regs_in_file)], matches=matches))
    return dict(ref=REFS[label], registration_count=len(regs), registrations=regs,
                file_writer_candidates=writers, registration_file_manifest=manifest)

def main():
    snapshots = {name: scan(Path(path), name) for name, path in zip(("main", "stable"), sys.argv[1:])}
    assert set(snapshots) == {"main", "stable"}, "Need both source paths"
    for name, result in snapshots.items():
        (OUT / f"source-registry-{name}.json").write_text(json.dumps(result, indent=2) + "\n")
    summary = {}
    for name, result in snapshots.items():
        backs = [x for x in result["registrations"] if x["registry_type"] == "Backend"]
        writes = [x for x in result["registrations"] if x["command"].startswith("write_")]
        summary[name] = dict(ref=REFS[name], backend_count=len(backs),
                             backend_commands=sorted(x["command"] for x in backs),
                             write_prefix_commands=sorted(x["command"] for x in writes))
        helps = []
        for item in backs + [x for x in writes if x not in backs]:
            helps.append(f"* {item['command']}\nSource: {item['url']}#L{item['registration_line']}\n\n#+begin_example\n{item['help_literals'] or '(no literal help extracted)'}#+end_example\n")
        (OUT / f"backend-help-{name}.org").write_text("#+title: Upstream Yosys " + name + " export command help literals\n\n" + "\n".join(helps))
    summary["same_backend_commands"] = summary["main"]["backend_commands"] == summary["stable"]["backend_commands"]
    (OUT / "registration-census.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
