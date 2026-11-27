# Pi SDK Role Harness (`user` and `reviewer`)

This directory provides the Node.js role harness built on `@earendil-works/pi-coding-agent@0.87.1` for the controller-side **User Simulator** (`user`) and **Rubric Reviewer** (`reviewer`).

## Installation & Unit Tests

Requires Node.js >= 22.19.0 and the project's Python virtual environment:

```bash
npm ci --prefix harnesses/pi --ignore-scripts
npm test --prefix harnesses/pi
uv run pytest -q tests/test_pi.py
```

## Execution Model

```text
Runner.call()
  -> PiAgent.run(Call)          (Python: prepares isolated workspace, role prompt, schema, and timeout)
  -> harnesses/pi/agent.mjs     (Node.js: runs an ephemeral in-memory Pi SDK session)
       -> model calls + read-only evidence tools
  -> Reply.text                 (validated by Runner against the role's output contract)
```

- Every invocation runs in a fresh, isolated in-memory session (`SessionManager.inMemory`) and never loads personal skills, user `AGENTS.md`, or Worker sessions.
- Stage orchestration stays strictly in Python (`multi_round.Runner`).

## Read-Only Tooling & Sandboxing

| File | Responsibility |
| --- | --- |
| `agent.mjs` | Ephemeral session lifecycle, structured output delivery (`tool`, `response_format`, or `text`), and event logging |
| `tools.mjs` | Path-jailed read-only workspace tools (`ls`, `read`, `read_json`, `read_excel`, `read_docx`, `read_pdf`, `read_pptx`, `read_conversation`) |
| `read_excel.py` | Read-only `.xlsx` sheet, formula, and cached-value inspector |
| `read_office.py` | Read-only `.docx`, `.pdf`, and `.pptx` paginated text/table extractor |
| `conversation.mjs` | Paginated reader for `evaluation/conversation.jsonl` (0-based message indices) |
| `sandbox-bash.mjs` | Optional offline, read-only Docker `bash` tool for Reviewer verification (`--read-only --network none`, writable only under `scratch/`) |
| `retry-fetch.mjs` | Single-layer HTTP retry wrapper for transient stream/gateway errors |

- **User Simulator (`user`)**:
  - `fixed` messages bypass the model completely.
  - `context` mode receives `workspace = null` (no file tools).
  - `workspace` mode receives read-only tools jailed to `user_workspace` (published materials + prior submitted deliverables).
- **Rubric Reviewer (`reviewer`)**:
  - Jailed to an immutable per-turn `review/` directory containing `evaluation/conversation.jsonl`, `evaluation/verification.json` (when programmatic checks ran), and `submission/` (when `needs_workspace=True`).

## Configuration Example

```json
{
  "type": "pi",
  "api": "openai-responses",
  "base_url": "https://api.openai.com/v1",
  "model": "gpt-6.2-sol",
  "thinking_level": "high",
  "key_env": "OPENAI_API_KEY",
  "max_tokens": 32768,
  "context_window": 131072,
  "max_model_turns": 128,
  "timeout": 1800,
  "supports_images": false
}
```

Supported `api` protocols are `"openai-responses"` (official OpenAI `/v1/responses` for GPT-6.x tool-calling roles), `"openai-completions"` (OpenAI-compatible `/v1/chat/completions` gateways), and `"anthropic-messages"` (Anthropic `/v1/messages`). Both `"openai-responses"` and `"anthropic-messages"` automatically deliver structured Reviewer output via the `submit_review_result` tool with strict TypeBox validation.
