"""Compatibility CLI for the fixed-configuration FIRRTL frontend."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = ROOT / "python"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from chisel2lean4.frontends import firrtl as _implementation

__doc__ = _implementation.__doc__
globals().update({name: value for name, value in vars(_implementation).items()
                  if not name.startswith("_")})


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
