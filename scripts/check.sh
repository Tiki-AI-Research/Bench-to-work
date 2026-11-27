#!/usr/bin/env bash
# Offline integrity & reference-solution check (no Docker, no LLM calls).
#   scripts/check.sh [<task_id>...]
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONUTF8=1
exec uv run --frozen python -m bench_to_work.check "$@"
