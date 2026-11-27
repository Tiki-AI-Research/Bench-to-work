#!/usr/bin/env bash
# End-to-end offline smoke test using real Runner + deterministic test doubles (no LLM calls).
#   scripts/smoke.sh [--harbor] [<task_id>...]
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONUTF8=1
exec uv run --frozen python -m bench_to_work.smoke "$@"
