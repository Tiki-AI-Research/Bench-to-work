# Agent & Harness Adapter Protocol

`multi_round.agents.make_agent` instantiates role adapters from JSON configuration without coupling the core scheduler to any vendor SDK.

## Built-in Adapter Types

| `type` | Typical Role | Description |
| --- | --- | --- |
| `codex` | Local CLI Worker | Invokes `@openai/codex` CLI (`codex exec` / `codex exec resume`), capturing JSONL events and resuming sessions across turns. |
| `pi` | Controller User / Reviewer | Invokes `harnesses/pi/agent.mjs` with read-only file/Office/PDF inspection tools (`read_excel`, `read_docx`, `read_pdf`, `read_pptx`) and optional read-only container sandbox `bash` for Reviewer recomputation. |
| `openai_compatible` | Progress Judge / Text User | Calls an OpenAI-compatible Chat Completions endpoint (`base_url`, `model`, `key_env`) with streaming and strict JSON schema validation where required. |
| `command` | Custom Worker / Smoke Doubles | Runs any external executable via JSON stdio: writes a single `Call` JSON to `stdin` and reads a single `Reply` JSON from `stdout`. |
| `python` | Custom Wrappers | Loads a `module:function` factory receiving `(project_path, **options)` (used by `bench_to_work.agents:make_review` and `make_text`). |

## Command Stdio Protocol

Custom agents (including `/opt/worker.py` inside Harbor containers) implement a minimal JSON contract:

**Input (`stdin`)**:
```json
{
  "call_id": "s1-01-worker",
  "run_id": "20261125-...",
  "role": "worker",
  "stage_id": "s1",
  "turn": 1,
  "prompt": "...",
  "instructions": "...",
  "workspace": "/workspace",
  "session_id": null,
  "history": [],
  "attachments": [{"path": "/workspace/data.xlsx", "kind": "file"}]
}
```

**Output (`stdout`)**:
```json
{
  "text": "Summary of completed work...",
  "session_id": "thread_abc123",
  "trace_refs": ["harbor-agent:native/s1-01-worker/events.jsonl"]
}
```

On follow-up turns (`history` non-empty), adapters must resume the prior `session_id` rather than silently reconstructing state from public chat text.

## Running a Custom Worker via `scripts/run.sh`

In `configs/<name>.yaml`, set `worker.harness: command` with `worker.command` (and optional `worker.files` to upload your agent scripts into the container at setup time, plus optional `worker.key_env` / `worker.env_vars` if your agent needs API credentials forwarded into the container). See [`examples/custom_worker/README.md`](../examples/custom_worker/README.md) and [`configs/custom-worker.yaml`](../configs/custom-worker.yaml) for a complete runnable template.
