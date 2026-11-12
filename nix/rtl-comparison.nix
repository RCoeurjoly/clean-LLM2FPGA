{ inputs, system, baseline, candidate }:
let pkgs = import inputs.nixpkgs { inherit system; };
in pkgs.runCommand "tinystories-rtl-comparison" {
  nativeBuildInputs = [ pkgs.python3 pkgs.diffutils ];
} ''
  mkdir "$out"
  ln -s ${baseline} "$out/baseline"
  ln -s ${candidate} "$out/torchao"
  cd "$out"
  for file in core.sv model.sv memory.sv; do
    status=0
    diff -u --label "baseline/$file" --label "torchao/$file" \
      "baseline/$file" "torchao/$file" > "$file.diff" || status=$?
    test "$status" -le 1
  done
  python - <<'PY'
  import hashlib
  import json
  import re
  from collections import Counter
  from pathlib import Path

  summary = {}
  for name in ('baseline', 'torchao'):
      root = Path(name)
      files = {}
      for path in sorted(root.rglob('*')):
          if path.is_file():
              data = path.read_bytes()
              files[str(path.relative_to(root))] = {
                  'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
              }
      memories = [(int(width), int(size)) for width, size in
                  re.findall(r'model_memory #\((\d+),(\d+),', (root / 'model.sv').read_text())]
      summary[name] = {'path': str(root.resolve()), 'files': files,
                       'rtl_bytes': sum(v['bytes'] for k, v in files.items() if k.endswith('.sv')),
                       'image_bytes': sum(v['bytes'] for k, v in files.items() if k.endswith('.hex')),
                       'memory_count': len(memories),
                       'memory_bits': sum(width * size for width, size in memories),
                       'memories_by_width': dict(sorted(Counter(width for width, _ in memories).items()))}
  Path('summary.json').write_text(json.dumps(summary, indent=2) + '\n')
  PY
''
