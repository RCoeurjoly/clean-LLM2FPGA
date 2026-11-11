"""Run the pinned sbt launcher with local caches and complete logs."""
import argparse
from pathlib import Path
from common import ROOT, run_logged, sbt_command

parser = argparse.ArgumentParser()
parser.add_argument("--log", default="experiments/build/latest")
parser.add_argument("commands", nargs="+", help="one argument per sbt command")
args = parser.parse_args()
command, environment = sbt_command(args.commands)
raise SystemExit(run_logged(command, ROOT / Path(args.log), env=environment))
