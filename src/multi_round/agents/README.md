# Built-in Role Adapters

`multi_round.agents` implements the standard `Agent.run(Call) -> Reply` interface used by `multi_round.Runner`. Adapters execute single role calls and never decide stage transitions.

| Module | Purpose |
| --- | --- |
| `__init__.py` | `make_agent()` factory dispatching by role config `type` |
| `command.py` | External process adapter speaking the JSON stdio `Call` / `Reply` protocol |
| `codex.py` | Local Codex CLI Worker adapter with native `session_id` resumption |
| `pi.py` | Python-to-Node bridge for `harnesses/pi/agent.mjs` (`user` and `reviewer`) |
| `openai_model.py` | Tool-free OpenAI-compatible Chat Completions adapter (commonly used for `progress_reviewer`) |
| `lifecycle.py` | Shared cleanup and error-recording helpers across CLI and Harbor runs |

Custom Python adapters can be plugged in directly via `"type": "python"` and `"factory": "package.module:callable"` without modifying `Runner`. See [Adapter Protocol](../../../docs/adapters.md) for details.
