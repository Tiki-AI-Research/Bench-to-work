# Harbor Container Execution & Isolation

Bench-to-Work uses [Harbor](https://pypi.org/project/harbor/) to run every benchmark trial inside a fresh, isolated Docker container while keeping orchestration and evaluation on the host controller.

## Isolation Boundary

| Component | Location | Access Scope |
| --- | --- | --- |
| `multi_round.Runner` | Host Controller | Full `spec/` directory, `rollout/` trusted logs |
| User Simulator / Progress Judge / Reviewer | Host Controller | Public conversation, stage prompt rules, frozen `review/` snapshot |
| `spec/verifier.py` | Host Controller | Frozen `turns/<stage>-<turn>/submission/` copy |
| Evaluated Worker (`codex` / `claude-code` / `command`) | Task Docker Container (`/workspace`) | Only `environment/` image contents, uploaded `worker.files`, and attachments published up to the current turn |

- **No future-stage leakage**: `spec/` is excluded from the Docker build context (`environment/`). Attachments for Stage `k` are uploaded into `/workspace` only when Stage `k` begins (or when a specific `continuation` triggers).
- **Least-privilege Worker credentials**: Task `task.toml` files do not forward host environment variables globally. `HarborWorker` injects only the credential variable(s) declared for the active Worker (`worker.key_env` / `worker.env_vars`), keeping host controller credentials outside the container.
- **No self-grading**: Container-side logs under `/logs/agent/native/` are never read by `verifier.py`, `Reviewer`, or `ReportVerifier`.
- **No web search shortcuts**: `/opt/worker.py` disables `web_search` / `multi_agent` in Codex and `WebSearch` / `WebFetch` in Claude Code by default so tasks measure reasoning over enterprise artifacts.

## Customizing Mirror Sources for Image Builds

`src/bench_to_work/templates/Dockerfile` uses official Debian, npm, and PyPI registries by default. When `DEBIAN_MIRROR`, `DEBIAN_SECURITY_MIRROR`, `NPM_REGISTRY`, or `PIP_INDEX_URL` are set in your host environment (or `.env`), `bench_to_work.job` automatically passes them as Docker Compose build arguments to Harbor.

## Benchmark Entry (`TaskAgent`) vs. Low-Level Bridge Fixture (`MainAgent`)

Benchmark jobs compiled by `scripts/run.sh` and `scripts/run_task.sh` invoke `bench_to_work.agent:TaskAgent`, which loads each task's private specification from `tasks/<id>/spec/task.json` and records two-tier `core_minor` metadata. By contrast, `tests/fixtures/harbor/` is a lightweight low-level `multi_round_harbor.agent:MainAgent` smoke fixture used by the test suite to exercise the Docker environment bridge directly rather than a benchmark task template.
