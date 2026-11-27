# Running a Custom Worker Through `scripts/run.sh`

External agents integrate with Bench-to-Work without modifying framework code by implementing the JSON stdio protocol (`Call` JSON on `stdin` -> `Reply` JSON on `stdout`; see [docs/adapters.md](../../docs/adapters.md)) and declaring `harness: command` in a job YAML under `configs/`.

## 1. How Your Agent Enters the Task Container

You have two ways to make your Worker code available inside the isolated task container:

- **Upload files/directories at container setup (`worker.files` — fastest for scripts & lightweight packages)**:
  List `{source, target}` mappings in your job YAML. Before Stage 1 begins, `MainAgent.setup` uploads those host files into a container-writable path outside `/workspace` (such as `/tmp/custom-worker/agent.py`, since the container runs as non-root UID `1000`). The task container's Python 3 environment (`/opt/worker-env`, including `openpyxl`, `python-docx`, `python-pptx`, and `pypdf`) and standard CLI tools remain available (headless LibreOffice runs on the host controller during verification).
- **Pre-install heavy dependencies in the container image**:
  If your agent requires custom system binaries or large wheels, install them in the task's `environment/Dockerfile` (or `src/bench_to_work/templates/Dockerfile`) and point `worker.command` at the installed entrypoint inside the container.

## 2. How to Write `configs/<your-job>.yaml`

See [`configs/custom-worker.yaml`](../../configs/custom-worker.yaml) for a minimal working configuration:

```yaml
dataset: tasks
task_names:
  - harborpoint-oct2026-sla-credit-handoff

worker:
  harness: command
  command:
    - python3
    - /tmp/custom-worker/agent.py
  files:
    - source: examples/custom_worker/agent.py
      target: /tmp/custom-worker/agent.py
  # Optional: only the variable names listed in key_env / env_vars are forwarded into the container.
  # Omit both when your agent needs no API keys.
  # key_env: MY_AGENT_API_KEY
  timeout: 300

# Use `configs/roles/offline-smoke.json` for keyless integration testing,
# or `configs/roles/pi-openai.json` (requires OPENAI_API_KEY on host) for live benchmark evaluation.
roles: configs/roles/offline-smoke.json
reward: bench_to_work.rewards:stage_scores
```

## 3. Commands to Run from the Repository Root

Preview the compiled Harbor `job.json` and `agent-config.json` (no API keys or Docker needed):

```bash
./scripts/run.sh --job custom-worker --dry-run
```

Run the custom Worker end-to-end inside a real Docker container via Harbor (no API keys needed when `roles` points to `configs/roles/offline-smoke.json`):

```bash
./scripts/run.sh --job custom-worker
# Or target a specific task explicitly:
./scripts/run_task.sh harborpoint-oct2026-sla-credit-handoff --job custom-worker
```
