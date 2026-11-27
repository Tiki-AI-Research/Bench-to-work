#!/usr/bin/env bash
# Run Bench-to-Work evaluation via Harbor + Docker.
#   scripts/run.sh [--job <name>] [--task <task_id>]... [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONUTF8=1
TMPDIR="$(cd "${TMPDIR:-/tmp}" && pwd -P)"
export TMPDIR

JOB=job
DRY=0
TASKS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --job) JOB="$2"; shift 2 ;;
    --task) TASKS+=(--task "$2"); shift 2 ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done

if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

CONFIG="configs/${JOB}.yaml"
[ -f "${CONFIG}" ] || { echo "Config not found: ${CONFIG}" >&2; exit 2; }

if [ "${DRY}" = 1 ]; then
  JOB_JSON=$(uv run --frozen python -m bench_to_work.job --job "${CONFIG}" ${TASKS[@]+"${TASKS[@]}"})
  echo "Harbor job config: ${JOB_JSON}"
  cat "${JOB_JSON}"
  echo "Agent config: $(dirname "${JOB_JSON}")/agent-config.json"
  cat "$(dirname "${JOB_JSON}")/agent-config.json"
  exit 0
fi

uv run --frozen python -m bench_to_work.job --job "${CONFIG}" --check-credentials
command -v docker >/dev/null || { echo "docker not found in PATH" >&2; exit 2; }
NEEDS_PI=$(uv run --frozen python -m bench_to_work.job --job "${CONFIG}" --needs-pi)
if [ "${NEEDS_PI}" = "1" ]; then
  command -v node >/dev/null || { echo "node (>=22.19) not found in PATH" >&2; exit 2; }
  [ -d "harnesses/pi/node_modules" ] || { echo "harnesses/pi/node_modules not found; run: npm ci --prefix harnesses/pi --ignore-scripts" >&2; exit 2; }
fi

JOB_JSON=$(uv run --frozen python -m bench_to_work.job --job "${CONFIG}" ${TASKS[@]+"${TASKS[@]}"})
echo "Harbor job config: ${JOB_JSON}"
uv run --frozen harbor run --config "${JOB_JSON}" --yes
