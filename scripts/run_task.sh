#!/usr/bin/env bash
# Run a single benchmark task via Harbor + Docker.
#   scripts/run_task.sh <task_id> [--job <name>] [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -lt 1 ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
  sed -n '2,3p' "$0"
  echo "Available tasks:"
  find tasks -name task.toml -not -path "*/spec/*" | sed -e 's#^tasks/##' -e 's#/task.toml$##' -e 's/^/  /' | sort
  exit 2
fi
TASK="$1"; shift
exec scripts/run.sh --task "${TASK}" "$@"
