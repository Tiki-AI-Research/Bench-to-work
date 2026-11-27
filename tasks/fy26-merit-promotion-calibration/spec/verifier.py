#!/usr/bin/env python3
"""Verifier for the FY26 merit & promotion calibration task.

Usage: verifier.py --workspace PATH --stage STAGE_ID
Trusted materials sit beside this file under their original relative paths; the
Worker submission is read (never modified) from --workspace.
"""
import argparse
import json
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE_DIR))

from verify.checks import Ctx, STAGE_CHECKS, collect_observations  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--stage", required=True, choices=sorted(STAGE_CHECKS))
    a = ap.parse_args(argv)
    ws = Path(a.workspace)
    if not ws.is_dir():
        raise SystemExit(f"workspace is not a directory: {ws}")
    ctx = Ctx(ws, a.stage, CODE_DIR)
    checks = [fn(ctx, cid) for cid, fn in STAGE_CHECKS[a.stage]]
    for c in checks:
        if not isinstance(c["passed"], bool):
            raise TypeError(f"{c['id']}: passed is not bool")
    obs = collect_observations(ctx)
    json.dump({"checks": checks, "observations": obs}, sys.stdout, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
