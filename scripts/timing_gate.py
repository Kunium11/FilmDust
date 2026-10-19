#!/usr/bin/env python3
"""Print the opt-in large-JPEG timing report.

The default test run does not execute this. A miss against 10 seconds is only
an error when FILMDUST_TIMING_ENFORCE=1. Machine speed changes the result.
Do not commit the photograph or the report.

Example:
  PYTHONPATH=filmdust .venv/bin/python scripts/timing_gate.py /path/to/scan.jpg
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "filmdust"))

from timing_gate import enforce_enabled, measure


def main(argv):
    args = [arg for arg in argv if not arg.startswith("-")]
    flags = {arg for arg in argv if arg.startswith("-")}
    sample = args[0] if args else os.environ.get("FILMDUST_TIMING_SAMPLE")
    if not sample:
        print("Pass a JPEG path, or set FILMDUST_TIMING_SAMPLE.", file=sys.stderr)
        return 2
    report = measure(sample)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    enforce = enforce_enabled() or "--enforce" in flags
    if enforce and not (report["first_compare_within_target"] and report["final_preview_within_target"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
