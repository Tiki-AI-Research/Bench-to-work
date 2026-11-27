# Bench-to-Work (v0.1)

**Bench-to-Work** evaluates how AI agents cross the gap from clean, self-contained benchmark prompts to real multi-stage office work. Instead of solving a fully specified exam problem in one shot, agents receive incomplete or evolving instructions over successive turns, clarify underspecified or conflicting requests, and revise deliverables—such as live-formula `.xlsx` workbooks and linked `.docx` memos—inside a persistent containerized workspace. Submissions are evaluated at each stage through deterministic programmatic checks (including headless LibreOffice formula recalculation) and constrained rubric-based review.

The initial **v0.1** release provides the full containerized evaluation framework and two multi-stage tasks: **FY26 Merit & Promotion Calibration** and **SLA Credit Claim & Cross-Team Handoff**. Additional tasks and broader comparative analyses will follow in subsequent releases.

---

## What Bench-to-Work Evaluates

1. **Stateful Multi-Stage Artifact Revision**
   - Agents work inside a persistent `/workspace` across 4–5 consecutive business stages rather than starting fresh each turn. Updates must preserve live spreadsheet formula chains, keep cross-file `.xlsx`/`.docx` figures synchronized, and leave unaffected sections intact.
2. **Inherited Errors, Evolving Scope, and Commitment Boundaries**
   - Tasks introduce realistic handoff friction demonstrated directly in the built-in suite: auditing an inherited workbook with a wrong contractual denominator while accounting for emails a predecessor already sent (`harborpoint-oct2026-sla-credit-handoff`), filtering current-year vs. deferred HR actions (`fy26-merit-promotion-calibration`), and holding outbound commitments while internal approvals or customer disputes remain unresolved.
3. **Host Controller / Worker Container Isolation**
   - **Host Controller**: Runs `multi_round.Runner`, the dynamic User Simulator (`fixed`, `context`, or artifact-reading `workspace` mode), the Progress Judge, `spec/verifier.py`, and the Rubric Reviewer. Future stage prompts, hidden checks, and reference solutions remain on the host under `spec/` and never enter the container image.
   - **Worker Container (Docker via Harbor)**: Runs only the evaluated Worker inside `/workspace`, receiving stage attachments incrementally and receiving only the API credential(s) declared for that Worker. Grading uses immutable controller-frozen `rollout/` snapshots rather than container-writable native logs.
4. **Two-Tier Outcome Scoring (`core` vs. `minor`)**
   - Each stage's checks are split in `spec/verify/design.json` (`rule = "core_minor"`) into **`core`** (must-pass delivery & core business judgments; failing any `core` item zeroes out the stage, `0.0`) and **`minor`** (non-fatal supplementary/routine items; evaluated only when all `core` items pass, deducting up to `minor_weight = 0.2` proportionally).
   - Both `task_passed` (`Task Pass@1`) and `stages_passed` check `core` items exclusively. Transport or infrastructure faults raise `status = "error"` back to the scheduler rather than silently recording `0` capability rewards.

---

## Repository Layout

```text
bench-to-work/
  src/
    multi_round/        Core multi-stage scheduler, data models, prompts, and role adapters (Codex, Pi, OpenAI API, Command)
    multi_round_harbor/ Harbor Docker environment bridge, HarborWorkspace, HarborWorker, and ReportVerifier
    bench_to_work/      Dataset discovery, Harbor job compiler, credential preflight, core/minor rewards, check, and smoke
  harnesses/pi/         Node.js Pi SDK role harness for tool-equipped User Simulator and Rubric Reviewer
  configs/              Harbor job configs (job.yaml, claude-code.yaml, custom-worker.yaml) and controller role configs
  scripts/              CLI entrypoints: check.sh, smoke.sh, run.sh, run_task.sh
  tasks/                Benchmark v0.1 tasks (see below)
  examples/             Custom Worker integration template (custom_worker/)
  docs/                 Architecture, adapter protocol, task authoring, and Harbor container execution guides
  tests/                Unit/integration test suite and local regression fixtures (tests/fixtures/)
```

---

## Included Benchmark v0.1 Tasks

1. **`fy26-merit-promotion-calibration`** — *FY26 Merit and Promotion Calibration* (5 stages, 17 programmatic checks + 14 rubric criteria)
   - Multi-sheet `.xlsx` live-formula compensation modeling + `.docx` executive summary maintenance across an HR Ops action batch (filtering FY26 vs. FY27 effective dates), conditional CFO/CPO/VP approvals, a pre-lock rating revision that triggers dual budget/ceiling gaps, and a uniform Product merit scaling factor (`0.9740914862`) interacting with promotion-first Band Max lump-sum caps.
2. **`harborpoint-oct2026-sla-credit-handoff`** — *October 2026 SLA Credit Claim & Cross-Team Handoff* (4 stages, 4 programmatic checks + 14 rubric criteria)
   - Uses **`workspace`-aware dynamic User Simulator turns** and **conditional `continuations`**: auditing an inherited case file (`43,200` 30-day denominator error vs. October's `44,640` 31-day MSA basis) while respecting already-sent customer email boundaries, modeling an SRE maintenance overlap scenario (`$2,400` vs. `$4,800`), holding outbound commitments during a customer contractual notice dispute, and closing out the approved `$4,800` Billing Credit Memo Request and final notice.

---

## Quickstart

### 1. Prerequisites & Installation

- **Python >= 3.12** and [uv](https://docs.astral.sh/uv/)
- **LibreOffice** (`soffice` in `PATH`, required on the host for headless spreadsheet formula recalculation)
- **Docker** (required for containerized Harbor runs)
- **Node.js >= 22.19** (required when running live LLM controller roles via `configs/roles/pi-openai.json`)

```bash
uv sync
npm ci --prefix harnesses/pi --ignore-scripts
[ -f .env ] || cp .env.example .env   # Initialize .env if absent, then fill API keys for your chosen job
```

### 2. Offline Verification & End-to-End Smoke Regression (No API Keys or Docker Needed)

Verify task integrity, container isolation boundaries, SHA-256 manifests, and run the programmatic verifiers against all stage reference solutions (`17/17` on `fy26-merit-promotion-calibration`, `4/4` on `harborpoint-oct2026-sla-credit-handoff`):

```bash
./scripts/check.sh
```

Run full multi-stage offline simulations through `multi_round.Runner` using deterministic test doubles (`reward=1.000`, `task_passed=1.000`):

```bash
./scripts/smoke.sh
```

### 3. Containerized Benchmark Evaluation (Harbor + Docker)

**Credential requirements** (checked upfront before Harbor starts; containers only receive the Worker's own credential):
- Default Codex job (`configs/job.yaml`): `OPENAI_API_KEY` (used by both the Codex Worker and host controller roles in `configs/roles/pi-openai.json`).
- Claude Code job (`configs/claude-code.yaml`): `ANTHROPIC_API_KEY` (forwarded only to the Claude Worker container) **and** `OPENAI_API_KEY` (used on the host by User Simulator, Progress Judge, and Reviewer).
- `--dry-run` and `configs/custom-worker.yaml` (with `configs/roles/offline-smoke.json`): **no API keys required**.
- **Model IDs & API protocols**: `configs/job.yaml` (`gpt-6.2-sol`), `configs/claude-code.yaml` (`claude-sonnet-5-5`), and `configs/roles/pi-openai.json` (User `gpt-6.1-terra` and Reviewer `gpt-6.2-sol` via OpenAI `openai-responses`; tool-less Progress Judge `gpt-6.2-sol` with `max_completion_tokens` and `temperature: null`) are configured for official OpenAI/Anthropic endpoints out of the box. When routing through a Chat Completions gateway instead, update `base_url`, `model`, and `api` (`openai-completions` or `anthropic-messages`) accordingly.
- **Multi-role invocation & API budget**: A live benchmark run involves up to four LLM roles (Worker, User Simulator, Progress Judge, and Rubric Reviewer); actual calls per turn depend on user message mode (`fixed` prompts skip the User model), whether the turn reaches `review_submission`, whether the stage declares rubric `criteria`, and retry counts. Run a single task via `./scripts/run_task.sh` first to gauge token usage before starting multi-task batches.

Preview the compiled Harbor `job.json` and controller `agent-config.json` without starting containers:

```bash
./scripts/run.sh --dry-run
```

Run a built-in CLI Worker in isolated Docker containers (choose one option):

```bash
# Option A: Codex Worker (configs/job.yaml, requires OPENAI_API_KEY)
./scripts/run_task.sh harborpoint-oct2026-sla-credit-handoff
```

```bash
# Option B: Claude Code Worker (configs/claude-code.yaml, requires ANTHROPIC_API_KEY + OPENAI_API_KEY)
./scripts/run_task.sh harborpoint-oct2026-sla-credit-handoff --job claude-code
```

```bash
# Run all tasks in tasks/ with the default Codex job
./scripts/run.sh
```

### 4. Integrating Your Own Custom Agent

Any executable speaking the JSON stdio protocol (`Call` JSON on `stdin` -> `Reply` JSON on `stdout`) can run directly through `scripts/run.sh` via `harness: command` without editing framework code. See [`examples/custom_worker/README.md`](examples/custom_worker/README.md) and [`configs/custom-worker.yaml`](configs/custom-worker.yaml):

```bash
./scripts/run.sh --job custom-worker --dry-run   # Inspect compiled Harbor + Worker upload config
./scripts/run.sh --job custom-worker             # Run multi-turn container verification (no API keys required)
```

Results are written to `jobs/<job_name>/<trial>/`:
- `result.json`: Harbor trial outcome and structured rewards (`reward`, `task_passed`, `stages_passed`, `items`, `checks`, `criteria`).
- `rollout/`: Trusted controller-side artifacts (`report.md`, `report.json`, and `turns/<stage>-<turn>/` containing frozen `submission/` snapshots, `verifier/result.json`, and `reviewer/` logs).

---

## Running Tests

```bash
uv run pytest -q
npm --prefix harnesses/pi test
```

## Documentation

- [Architecture & Stage State Machine](docs/architecture.md)
- [Agent & Harness Adapter Protocol](docs/adapters.md)
- [Custom Worker Integration Example](examples/custom_worker/README.md)
- [Task Authoring & Verification Guide](docs/task-authoring.md)
- [Harbor Container Execution & Isolation](docs/harbor-execution.md)

## License

Released under the [Apache License 2.0](LICENSE).
