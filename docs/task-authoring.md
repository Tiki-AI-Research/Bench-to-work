# Task Authoring & Verification Guide

Each Bench-to-Work task lives under `tasks/<task_id>/` with a clean separation between the isolated Worker container (`environment/`) and the host-only specification and grading logic (`spec/`).

## 1. Directory Layout (`tasks/<task_id>/`)

```text
tasks/<task_id>/
  task.toml             Harbor task metadata (`task_spec = "spec/task.json"`) and container resource limits
  instruction.md        Human/Harbor summary (stage prompts are sent dynamically by the controller)
  README.md             Stage overview and deliverables
  environment/          Isolated Worker container build context: Dockerfile, worker.py, requirements.txt, public/ hooks
  tests/test.sh         Harbor placeholder (evaluation runs on the controller side)
  spec/                 Controller-only task package (never mounted into the Worker container):
    task.json           Multi-stage specification, User Simulator rules, verifier_checks, and rubric criteria
    verifier.py         Deterministic programmatic verifier
    verify/             Verification helpers, reference solutions (`verify/reference/<stage>/`), and `design.json`
    manifest.json       SHA-256 file integrity manifest
```

## 2. Core Specification (`spec/task.json`)

A task defines:
- `id`, `title`, `persona`, `background` (context for the User Simulator; never shown directly to the Worker).
- `stages`: Ordered list of `Stage` objects, each specifying:
  - `id`, `title`, `max_turns`, `retry_limit`, `needs_workspace`.
  - `initial`: Opening `Action` (`message` with `type` in `fixed | context | workspace`, plus `attachments` published to `/workspace`).
  - `continuations`: Conditional User Simulator response rules (`id`, `when`, `message`, optional `attachments`) triggered when the Worker asks clarifying questions or pushes back on an ambiguous instruction.
  - `verifier_checks`: Programmatic check declarations (`id`, `description`, `hard_gate`).
  - `criteria`: Semantic rubric items (`id`, `description`, optional `verification` guidance for the Reviewer).
- `verifier`: Command template executed on the controller side, e.g. `["{python}", "{task}/verifier.py", "--workspace", "{workspace}", "--stage", "{stage}"]`.

## 3. Programmatic Verifier Contract (`spec/verifier.py`)

Bench-to-Work intentionally decouples task orchestration from internal verifier structure: a task may implement its checks directly in idiomatic Python modules (like the built-in tasks) or drive reusable check libraries from a declarative JSON table. The only contract is that `spec/verifier.py` inspects the frozen submission copy (`{workspace}`) and writes a JSON object to `stdout`:

```json
{
  "checks": [
    {"id": "s1_chk_pools", "passed": true, "evidence": "All 5 department pool rows match."}
  ],
  "observations": {
    "workbook_summary": "..."
  }
}
```

- **Distinguish Worker mistakes from verifier faults**: If a Worker omits a file, hardcodes a formula, or writes wrong numbers, exit `0` and set `"passed": false` with clear `evidence`. If the verifier encounters a missing host dependency (e.g., `soffice`) or missing trusted material, exit non-zero so `Runner` records `status = "error"` rather than mischarging the Worker.
- **Cross-stage comparisons**: Read prior accepted snapshots from `{turn_dir}/../../turns/<prev_stage>-<turn>/submission/` rather than relying on mutable container state.

## 4. Two-Tier Outcome Scoring (`spec/verify/design.json`)

To prevent routine formatting checks from diluting core decision-making signal, define `spec/verify/design.json` with `rule = "core_minor"` (`minor_weight = 0.2` by default):

- **`core`**: Must-pass delivery prerequisites and core business judgments for the stage. If **any** `core` check fails, the stage score is **`0.0`**.
- **`minor`**: Non-fatal supplementary or routine requirements. Evaluated **only when all `core` checks in the stage pass**, deducting up to `minor_weight` (`0.2`) proportionally (`stage_score = 0.8 + 0.2 * minor_pass_rate`, or `1.0` if the stage has no `minor` checks).
- **`task_passed` (`Task Pass@1`) & `stages_passed`**: Determined solely by `core` checks—minor omissions slightly reduce `reward` without turning a sound stage or task into a `0` pass.

## 5. Offline Validation Checklist

1. Place stage reference solutions under `spec/verify/reference/<stage_id>/`.
2. Record SHA-256 hashes of all files under `spec/` in `spec/manifest.json`.
3. Run offline checks and end-to-end smoke regression:
   ```bash
   ./scripts/check.sh <task_id>
   ./scripts/smoke.sh <task_id>
   ```
