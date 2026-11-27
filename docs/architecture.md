# Architecture & Stage State Machine

Bench-to-Work separates **stage orchestration**, **role execution**, and **task-specific verification** into clean, non-overlapping layers.

## Roles in a Run

Each multi-round run coordinates four logical roles:

1. **User Simulator (`user`)**: Produces the stage opening message (`fixed` literal text, `context` dialogue-conditioned generation, or `workspace` artifact-aware generation reading prior delivered files from `user_workspace`) and responds to intra-stage Worker clarifications according to `continuations` rules and `default_reply`. Attachments are published deterministically by the `Runner`, never guessed by the User model.
2. **Worker (`worker`)**: The evaluated agent operating inside an isolated workspace (`/workspace` in Docker or a local directory in CLI mode). Maintains its native tool loop and session across stages (`session_id`).
3. **Progress Judge (`progress_reviewer`)**: A lightweight text-only role inspecting the public conversation after each Worker reply to decide between `request_user_reply` (continue intra-stage dialogue) and `review_submission` (freeze workspace and run formal stage evaluation).
4. **Evaluator (`verifier` + `reviewer`)**:
   - **Programmatic Verifier (`spec/verifier.py`)**: Deterministic script checking formulas, values, cross-file consistency, and untouched-scope rules on the frozen `submission/` snapshot, optionally emitting structured `observations` for the Reviewer.
   - **Rubric Reviewer (`reviewer`)**: Tool-equipped read-only agent (mounted only on the frozen `review/` directory) evaluating semantic `criteria` into strict schema-validated JSON.

## Stage Lifecycle

Within each stage `s_i`:

1. `Runner` triggers `before_stage` hooks, publishes initial attachments to the Worker workspace, and delivers the initial User message.
2. The Worker executes in its workspace and returns a reply (`after_worker` hooks such as preview extraction run if configured).
3. If `max_turns` has not been reached, the Progress Judge decides whether the Worker is asking a clarifying question (`request_user_reply`) or presenting a deliverable (`review_submission`).
4. On `review_submission`:
   - If `needs_workspace=True`, `Runner` freezes an immutable copy of the Worker workspace into `turns/<stage>-<turn>/submission/` with SHA-256 `manifest.json`.
   - `Runner` executes `verifier.py` (if `verifier_checks` or observations are configured) and invokes `Reviewer` (if `criteria` are configured).
   - If any `hard_gate=True` check fails and `retry_count < retry_limit`, `Runner` sends a retry notice and allows another attempt within the stage; if `retry_limit` is exhausted on a hard gate, the run stops as `failed`.
   - Non-gate check failures are recorded (`advanced_with_errors`) and the run advances to `s_{i+1}`, reflecting real workflows where subtle mistakes compound or require downstream handling.
5. Transport, process, or verifier crashes raise `AdapterError` / `ExternalExecutionError`, halting the run with `status = "error"` so infrastructure faults are never conflated with agent capability failures.
