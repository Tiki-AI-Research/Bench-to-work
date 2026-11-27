"""Single stage scheduler. Read run() and run_stages() for the full loop from user turn to stage progression."""

import json
import sys
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path, PurePath
from typing import TypeVar
from uuid import uuid4

from .enums import Phase, Role
from .json_protocol import parse_and_validate_json
from .models import (
    PROGRESS_SCHEMA,
    REVIEW_SCHEMA,
    VERIFIER_SCHEMA,
    Action,
    Agent,
    Attachment,
    Call,
    Check,
    IncompleteResponseError,
    OutputProtocolError,
    Reply,
    Stage,
    Task,
    UserMessage,
    inside,
)
from .processes import run_process
from .prompts import (
    RETRY_MESSAGE,
    RETRY_MESSAGE_ZH,
    progress_prompt,
    resolve_public_language,
    retry_message_for,
    reviewer_correction_prompt,
    reviewer_prompt,
    user_prompt,
    worker_instructions,
)
from .record_types import (
    CallRecord,
    CheckResult,
    HookRecord,
    RunRecord,
    TurnRecord,
    VerificationResult,
)
from .records import (
    describe_error,
    evaluation_scope,
    invoke_agent,
    parse_model_reply,
    record_protocol_error,
    save_report,
)
from .storage import (
    archive_attachments,
    conversation_images,
    prepare_review_workspace,
    receive_attachments,
    receive_submission,
    write_json,
)
from .workspaces import LocalWorkspace, WorkerWorkspace

T = TypeVar("T")


def validate_check_results(
    actual: list[CheckResult], expected: Sequence[Check]
) -> list[CheckResult]:
    """Verify check IDs are complete and unique, then attach task-defined hard_gate flags in task order."""
    ids = [c["id"] for c in actual]
    if len(set(ids)) != len(ids) or set(ids) != {c.id for c in expected}:
        raise OutputProtocolError(
            f"Missing, duplicate, or unexpected check IDs: got {ids}, expected {[c.id for c in expected]}"
        )
    by_id = {c["id"]: c for c in actual}
    # hard_gate comes strictly from the task definition so a model cannot downgrade a failing gate.
    return [{**by_id[c.id], "hard_gate": c.hard_gate} for c in expected]


def validate_progress_result(result: dict, stage: Stage) -> None:
    """continuation_id membership is an output-protocol constraint; a valid 'error' decision is not retried as malformed."""
    reply_id = result["continuation_id"]
    if result["decision"] == "request_user_reply":
        if reply_id is not None and reply_id not in {a.id for a in stage.continuations}:
            raise OutputProtocolError(
                "Progress review requested an undefined continuation_id"
            )
    elif reply_id is not None:
        raise OutputProtocolError(
            "Non-request_user_reply progress decision must have continuation_id=null"
        )


class Runner:
    def __init__(
        self,
        user: Agent,
        worker: Agent,
        reviewer: Agent,
        verifier_timeout: int = 60,
        progress=print,
        progress_reviewer: Agent | None = None,
        hook_handlers: dict[str, Callable[[dict], None]] | None = None,
        reviewer_protocol_retry_limit: int = 1,
        public_language: str | None = None,
    ):
        if (
            type(reviewer_protocol_retry_limit) is not int
            or reviewer_protocol_retry_limit < 0
        ):
            raise ValueError(
                "reviewer_protocol_retry_limit must be a non-negative integer"
            )
        if public_language is not None and public_language not in {"en", "zh"}:
            raise ValueError("public_language must be 'en' or 'zh'")
        self.reviewer_protocol_retry_limit = reviewer_protocol_retry_limit
        self.public_language = public_language
        self.user, self.worker, self.reviewer = user, worker, reviewer
        # Progress review and formal evaluation are configured independently; when omitted, they share the adapter via separate calls.
        self.progress_reviewer = (
            progress_reviewer if progress_reviewer is not None else reviewer
        )
        self.verifier_timeout, self.progress = verifier_timeout, progress
        # In-process hook handlers supplied by callers; Runner holds only generic event callbacks.
        self.hook_handlers = dict(hook_handlers or {})

    def call(
        self,
        agent: Agent,
        role: Role | str,
        prompt: str,
        workspace: PurePath | None,
        log_dir: Path,
        schema: dict | None = None,
        session_id: str | None = None,
        history: list[dict] | None = None,
        instructions: str = "",
        attachments: list[Attachment] | None = None,
        *,
        context: dict | None = None,
        calls: list[CallRecord] | None = None,
        phase: Phase = Phase.UNSPECIFIED,
        run_dir: Path | None = None,
    ) -> Reply:
        # Pass deep copies so adapter-side context formatting cannot mutate the runner's canonical conversation.
        call = Call(
            role,
            prompt,
            workspace,
            log_dir,
            session_id,
            schema,
            history=deepcopy(history or []),
            instructions=instructions,
            attachments=deepcopy(attachments or []),
            **(context or {}),
            call_id=uuid4().hex,
            phase=phase,
        )
        return invoke_agent(
            agent, call, calls=calls, run_dir=run_dir, progress=self.progress
        )

    def review(
        self,
        agent: Agent,
        prompt: str,
        workspace: Path | None,
        log_dir: Path,
        schema: dict,
        *,
        validate: Callable[[dict], T],
        context: dict,
        calls: list[CallRecord],
        phase: Phase,
        run_dir: Path,
        attachments: list[Attachment] | None = None,
    ) -> tuple[dict, T]:
        """Return raw structured output and validated result; incomplete responses and protocol fixes share a bounded retry budget without re-running Worker."""
        original_prompt, original_call_id = prompt, None
        protocol_retry = 0
        incomplete_retry = 0
        attempt_dir = log_dir
        while True:
            try:
                reply = self.call(
                    agent,
                    phase.agent_role,
                    prompt,
                    workspace,
                    attempt_dir,
                    schema,
                    attachments=attachments,
                    context={
                        **context,
                        "protocol_retry_count": protocol_retry,
                        "protocol_retry_of": original_call_id if protocol_retry else None,
                    },
                    calls=calls,
                    phase=phase,
                    run_dir=run_dir,
                )
            except IncompleteResponseError:
                if (
                    protocol_retry + incomplete_retry
                    >= self.reviewer_protocol_retry_limit
                ):
                    raise
                incomplete_retry += 1
                attempt_dir = log_dir / f"incomplete_retry_{incomplete_retry}"
                continue
            record = calls[-1]
            if original_call_id is None:
                original_call_id = record["call_id"]
            try:
                result = parse_model_reply(
                    reply, schema, record=record, run_dir=run_dir
                )
                return result, validate(result)
            except OutputProtocolError as exc:
                record_protocol_error(exc, record=record, run_dir=run_dir)
                if (
                    protocol_retry + incomplete_retry
                    >= self.reviewer_protocol_retry_limit
                ):
                    raise
                protocol_retry += 1
                prompt = reviewer_correction_prompt(
                    original_prompt,
                    reply.text,
                    str(exc),
                )
                attempt_dir = log_dir / f"protocol_retry_{protocol_retry}"

    def resolve_user_message(
        self,
        task: Task,
        stage: Stage,
        action: Action,
        messages: list,
        *,
        run_dir: Path,
        log_dir: Path,
        visible_files: list[str],
        user_attachments: list[dict],
        report: RunRecord,
        context: dict,
        calls: list[CallRecord],
    ) -> str:
        """Obtain the user message text and record any User call; does not publish files, mutate history, or advance stages."""
        match action.message.type:
            case "fixed":
                return action.message.text
            case "context":
                workspace = None
                images = []
            case "workspace":
                workspace = run_dir / "user_workspace"
                # Prior and current user images are passed only in workspace mode; evaluation records are never exposed to User.
                images = conversation_images(report, run_dir) + [
                    Attachment(str(run_dir / attachment["saved_path"]), "image")
                    for attachment in user_attachments
                    if attachment["kind"] == "image"
                ]
            case _:
                raise ValueError(f"Unknown user message type: {action.message.type}")

        prompt = self.build_user_prompt(
            task,
            stage,
            action,
            messages,
            visible_files=visible_files,
            current_attachments=[
                {"path": a["path"], "kind": a["kind"]} for a in user_attachments
            ],
        )
        phase = Phase.USER_MESSAGE
        reply = self.call(
            self.user,
            phase.agent_role,
            prompt,
            workspace,
            log_dir,
            attachments=images,
            context=context,
            calls=calls,
            phase=phase,
            run_dir=run_dir,
        )
        # User generates message text only; attachments are published deterministically from Action config.
        if not reply.text.strip():
            raise OutputProtocolError("User must return a non-empty message body")
        return reply.text

    def run_hooks(
        self,
        task: Task,
        event: Phase | str,
        run_dir: Path,
        report: RunRecord,
        *,
        stage_id: str | None = None,
        turn: int | None = None,
        action_id: str | None = None,
        workspace: WorkerWorkspace | None = None,
    ) -> None:
        """Execute matching task commands or callbacks at lifecycle events with unified logging and error handling."""
        workspace = workspace or LocalWorkspace(run_dir / "workspace")
        failures = []
        for hook in task.hooks:
            if hook.event != event:
                continue
            if hook.stage_id is not None and hook.stage_id != stage_id:
                continue
            if hook.turn is not None and hook.turn != turn:
                continue
            if hook.action_id is not None and hook.action_id != action_id:
                continue
            log_dir = run_dir / "hooks" / f"{len(report['hooks']) + 1:03d}-{hook.id}"
            record: HookRecord = {
                "id": hook.id,
                "event": event,
                "stage_id": stage_id,
                "turn": turn,
                "action_id": action_id,
                "description": hook.description,
                "status": "running",
                "log_dir": str(log_dir.relative_to(run_dir)),
            }
            report["hooks"].append(record)
            context = {
                "event": event,
                "stage_id": stage_id,
                "turn": turn,
                "action_id": action_id,
                "workspace": str(workspace.root),
                "turn_dir": (
                    str(run_dir / "turns" / f"{stage_id}-{turn:02d}")
                    if stage_id is not None and turn is not None
                    else None
                ),
                "log_dir": str(log_dir),
                "run_status": report["status"],
            }
            try:
                if hook.handler is None:
                    context = workspace.hook_context(context)
                write_json(log_dir / "request.json", context)
                if hook.handler is not None:
                    if hook.handler not in self.hook_handlers:
                        raise ValueError(f"Unregistered hook handler: {hook.handler}")
                    self.hook_handlers[hook.handler](context)
                else:
                    replacements = {
                        **workspace.hook_bindings(task.root),
                        "{workspace}": context["workspace"],
                    }
                    command = list(hook.command)
                    for key, value in replacements.items():
                        command = [arg.replace(key, value) for arg in command]
                    workspace.execute(
                        command,
                        stdin=json.dumps(context),
                        timeout=hook.timeout,
                        log_dir=log_dir,
                    )
                record["status"] = "completed"
            except (Exception, KeyboardInterrupt) as exc:
                record["error_details"] = describe_error(exc, phase=Phase(event))
                record["status"], record["error"] = (
                    "error",
                    f"{type(exc).__name__}: {exc}",
                )
                if isinstance(exc, KeyboardInterrupt):
                    report["interrupted"] = True
                if event != Phase.AFTER_RUN:
                    raise
                # Continue remaining after_run hooks even if one fails so scheduled resources are still released.
                failures.append(record["error"])
        if failures:
            raise RuntimeError("after_run hook failed: " + "; ".join(failures))

    def verify(
        self, task: Task, stage: Stage, submitted: Path, log_dir: Path
    ) -> VerificationResult:
        replacements = {
            "{python}": sys.executable,
            "{task}": str(task.root),
            "{workspace}": str(submitted),
            "{stage}": stage.id,
            "{turn_dir}": str(log_dir.parent),
        }
        command = list(task.verifier)
        for key, value in replacements.items():
            command = [arg.replace(key, value) for arg in command]
        result = run_process(
            command,
            cwd=submitted,
            stdin="",
            timeout=self.verifier_timeout,
            log_dir=log_dir,
        )
        data = parse_and_validate_json(result, VERIFIER_SCHEMA)
        data["checks"] = validate_check_results(data["checks"], stage.verifier_checks)
        write_json(log_dir / "result.json", data)
        return data

    def build_user_prompt(
        self,
        task: Task,
        stage: Stage,
        action: Action,
        messages: list,
        *,
        visible_files: list[str],
        current_attachments: list[dict],
    ) -> str:
        return user_prompt(
            task,
            stage,
            action,
            messages,
            visible_files=visible_files,
            current_attachments=current_attachments,
        )

    def build_progress_prompt(
        self, stage: Stage, messages: list, *, stage_start_message: int
    ) -> str:
        return progress_prompt(
            stage, messages, stage_start_message=stage_start_message
        )

    def should_collect_reply_files(self, stage: Stage) -> bool:
        return stage.needs_workspace

    def collect_reply_files(
        self,
        task: Task,
        stage: Stage,
        workspace: WorkerWorkspace,
        user_workspace: Path,
        turn: TurnRecord,
        turn_dir: Path,
        run_dir: Path,
    ) -> Path:
        intermediate = turn_dir / "intermediate"
        if stage.needs_workspace:
            hashes = workspace.collect(task.submission_paths, intermediate)
            turn["reply_snapshot"] = str(intermediate.relative_to(run_dir))
            write_json(turn_dir / "intermediate_manifest.json", hashes)
        return intermediate

    def promote_submission(
        self,
        task: Task,
        stage: Stage,
        intermediate: Path,
        user_workspace: Path,
        turn: TurnRecord,
        turn_dir: Path,
        run_dir: Path,
    ) -> Path | None:
        if stage.needs_workspace:
            submitted = turn_dir / "submission"
            intermediate.rename(submitted)
            turn["snapshot"] = turn["reply_snapshot"] = str(
                submitted.relative_to(run_dir)
            )
            (turn_dir / "intermediate_manifest.json").rename(turn_dir / "manifest.json")
            receive_submission(submitted, user_workspace, task.submission_paths)
            return submitted
        if stage.verifier_checks:
            raise ValueError("Inconsistent stage evidence configuration")
        return None

    def stage_outcome(self, stage: Stage, failed: list[CheckResult]) -> str:
        return "advanced_with_errors" if failed else "passed"

    def complete_run(self, task: Task, report: RunRecord) -> None:
        report["status"], report["reason"] = (
            "completed",
            "All stages evaluated; hard gates passed",
        )

    def save_run_report(self, run_dir: Path, report: RunRecord) -> None:
        save_report(run_dir, report)

    def run(
        self,
        task: Task,
        run_dir: Path,
        mode: str = "live",
        *,
        run_config: dict | None = None,
        frozen_design: dict | None = None,
        workspace: WorkerWorkspace | None = None,
    ) -> RunRecord:
        """Execute stages and finalize uniformly; cleanup and report saving run on completion, early stop, and exceptions."""
        run_dir = run_dir.resolve()
        run_dir.mkdir(parents=True, exist_ok=False)  # Never overwrite an existing run directory.
        # Save caller-provided configuration snapshot as-is without interpreting adapter wiring here.
        if run_config is not None:
            write_json(run_dir / "config.json", run_config)
        workspace = workspace or LocalWorkspace(run_dir / "workspace")
        user_workspace = run_dir / "user_workspace"
        user_workspace.mkdir()
        write_json(run_dir / "task.json", {**asdict(task), "root": str(task.root)})
        if frozen_design is not None:
            write_json(run_dir / "design.json", frozen_design)
        report: RunRecord = {
            "run_id": uuid4().hex,
            "task_id": task.id,
            "title": task.title,
            "mode": mode,
            "status": "running",
            "reason": "Running",
            "hooks": [],
            "stages": [
                {"id": s.id, "title": s.title, "status": "pending", "turns": []}
                for s in task.stages
            ],
        }
        try:
            self.run_stages(task, run_dir, report, workspace)
        finally:
            # after_run executes once for the whole run (including early stops and exceptions) to release resources.
            try:
                self.run_hooks(
                    task, Phase.AFTER_RUN, run_dir, report, workspace=workspace
                )
            except Exception as exc:
                report["cleanup_error"] = str(exc)
                report["cleanup_error_details"] = describe_error(
                    exc, phase=Phase.AFTER_RUN
                )
                if report["status"] == "completed":
                    report["status"], report["reason"] = "error", str(exc)
            finally:
                # Final status may change after the last turn (budget exhaustion, completion, or cleanup failure).
                self.save_run_report(run_dir, report)
        return report

    def run_stages(
        self, task: Task, run_dir: Path, report: RunRecord, workspace: WorkerWorkspace
    ) -> None:
        """Main stage loop: in-stage turns and evaluation, followed by advance, retry, or early stop."""
        user_workspace = run_dir / "user_workspace"
        messages, worker_session = [], None
        released_paths: set[str] = set()
        for stage, stage_log in zip(task.stages, report["stages"]):
            self.progress(f"Stage {stage.id}: {stage.title}")
            stage_log["status"] = "running"
            stage_log["start_message"] = len(messages)
            # Initialize once per stage; inner-loop continues neither resend initial nor reset retry_count.
            action, retry_count = stage.initial, 0
            for turn_no in range(1, stage.max_turns + 1):
                turn: TurnRecord = {
                    "turn_id": uuid4().hex,
                    "stage_id": stage.id,
                    "turn": turn_no,
                    "action_id": action.id,
                    "calls": [],
                }
                stage_log["turns"].append(turn)
                context = {
                    "run_id": report["run_id"],
                    "stage_id": stage.id,
                    "turn_id": turn["turn_id"],
                    "turn": turn_no,
                    "retry_count": retry_count,
                }
                turn_dir = run_dir / "turns" / f"{stage.id}-{turn_no:02d}"
                phase = Phase.RELEASE_MATERIALS
                try:
                    # 1. Release only materials attached to the current action; future materials stay in the task folder.
                    turn["released_attachments"] = []
                    incoming = []
                    for attachment in action.attachments:
                        name = attachment.path
                        # Continuations may repeat; already-published files are never overwritten or restored.
                        if name in released_paths:
                            continue
                        workspace.publish(inside(task.root, name), name)
                        released_paths.add(name)
                        turn["released_attachments"].append(name)
                        incoming.append(attachment)

                    # before_user / after_worker belong to each turn and fire again on follow-ups and retries.
                    phase = Phase.BEFORE_USER
                    self.run_hooks(
                        task,
                        phase,
                        run_dir,
                        report,
                        stage_id=stage.id,
                        turn=turn_no,
                        action_id=action.id,
                        workspace=workspace,
                    )
                    phase = Phase.USER_ATTACHMENTS
                    user_attachments = archive_attachments(
                        incoming,
                        workspace.fetch_file,
                        turn_dir / "attachments/user",
                        run_dir,
                    )
                    receive_attachments(user_attachments, run_dir, user_workspace)
                    visible_files = sorted(
                        str(p.relative_to(user_workspace))
                        for p in user_workspace.rglob("*")
                        if p.is_file()
                    )
                    turn["user_visible_files"] = visible_files

                    # 2. Resolve the user message and pass it to Worker.
                    turn["user_message_type"] = action.message.type
                    phase = Phase.USER_MESSAGE
                    turn["user"] = self.resolve_user_message(
                        task,
                        stage,
                        action,
                        messages,
                        run_dir=run_dir,
                        log_dir=turn_dir / "user",
                        visible_files=visible_files,
                        user_attachments=user_attachments,
                        report=report,
                        context=context,
                        calls=turn["calls"],
                    )
                    turn["user_attachments"] = user_attachments
                    user_message = {"role": "user", "content": turn["user"]}
                    if incoming:
                        user_message["attachments"] = [asdict(a) for a in incoming]
                    messages.append(user_message)
                    # prompt carries only the new user turn; history is passed separately for stateless adapters.
                    # Snapshot retry_count once Worker execution begins; failed user-generation turns leave retry_count unset.
                    turn["retry_count"] = retry_count
                    phase = Phase.WORKER
                    worker = self.call(
                        self.worker,
                        phase.agent_role,
                        turn["user"],
                        workspace.root,
                        turn_dir / "worker",
                        session_id=worker_session,
                        history=messages[:-1],
                        instructions=worker_instructions(),
                        attachments=[
                            Attachment(str(workspace.root / a.path), a.kind)
                            for a in incoming
                        ],
                        context=context,
                        calls=turn["calls"],
                        phase=phase,
                        run_dir=run_dir,
                    )
                    worker_session = worker.session_id
                    turn["assistant"] = worker.text
                    # Paths mentioned in reply text are informational only; workspace files are collected separately by Runner.
                    messages.append({"role": "assistant", "content": worker.text})
                    phase = Phase.AFTER_WORKER
                    self.run_hooks(
                        task,
                        phase,
                        run_dir,
                        report,
                        stage_id=stage.id,
                        turn=turn_no,
                        action_id=action.id,
                        workspace=workspace,
                    )

                    # Snapshot files produced with this reply; User exposure and formal promotion happen only upon submission.
                    intermediate = turn_dir / "intermediate"
                    if self.should_collect_reply_files(stage):
                        phase = Phase.PREPARE_SUBMISSION
                        intermediate = self.collect_reply_files(
                            task,
                            stage,
                            workspace,
                            user_workspace,
                            turn,
                            turn_dir,
                            run_dir,
                        )

                    # 3. Progress review inspects public conversation only.
                    phase = Phase.PROGRESS_REVIEW
                    progress, _ = self.review(
                        self.progress_reviewer,
                        self.build_progress_prompt(
                            stage,
                            messages,
                            stage_start_message=stage_log["start_message"],
                        ),
                        None,
                        turn_dir / "progress",
                        PROGRESS_SCHEMA,
                        validate=lambda result: validate_progress_result(result, stage),
                        context=context,
                        calls=turn["calls"],
                        phase=phase,
                        run_dir=run_dir,
                    )
                    turn["progress"] = progress

                    # 4. Continue within the current stage using the matched continuation rule or default_reply.
                    decision = progress["decision"]
                    if decision == "request_user_reply":
                        choices = {a.id: a for a in stage.continuations}
                        reply_id = progress["continuation_id"]
                        if reply_id is None:
                            action = Action(
                                "default_reply",
                                stage.default_reply,
                                when="No continuation rule matched",
                            )
                        else:
                            action = choices[reply_id]
                        # Next turn resolves the selected user action and resumes Worker's native session.
                        continue
                    if decision == "error":
                        raise ValueError(progress["reason"])

                    # 5. needs_workspace controls Worker file snapshotting; every formal evaluation gets an isolated review directory.
                    turn["evaluation_id"] = uuid4().hex
                    turn["evaluation_scope"] = evaluation_scope(
                        stage_log, len(messages)
                    )
                    phase = Phase.PREPARE_SUBMISSION
                    submitted = self.promote_submission(
                        task,
                        stage,
                        intermediate,
                        user_workspace,
                        turn,
                        turn_dir,
                        run_dir,
                    )

                    review_workspace = turn_dir / "review"
                    prepare_review_workspace(review_workspace, messages, submitted)
                    turn["review_workspace"] = str(
                        review_workspace.relative_to(run_dir)
                    )

                    # 6. Run declared programmatic and semantic checks before deciding stage progression.
                    verification = None
                    if stage.verifier_checks:
                        phase = Phase.VERIFICATION
                        verification = self.verify(
                            task,
                            stage,
                            review_workspace / "submission",
                            turn_dir / "verifier",
                        )
                        turn["verification"] = verification
                        # Store full verifier output (including observations) inside the review workspace rather than bloating the prompt.
                        write_json(
                            review_workspace / "evaluation/verification.json",
                            verification,
                        )
                    program_checks = verification["checks"] if verification else []
                    reviewer_checks = []
                    if stage.criteria:
                        phase = Phase.EVALUATION
                        # needs_workspace controls whether submission files exist, not which Reviewer backend runs.
                        review, reviewer_checks = self.review(
                            self.reviewer,
                            reviewer_prompt(
                                stage,
                                messages,
                                verification,
                                stage_turn=turn_no,
                                retry_count=retry_count,
                                stage_start_message=stage_log["start_message"],
                                events=report["hooks"],
                            ),
                            review_workspace,
                            turn_dir / "reviewer",
                            REVIEW_SCHEMA,
                            validate=lambda result: validate_check_results(
                                result["criteria"], stage.criteria
                            ),
                            attachments=conversation_images(report, run_dir),
                            context=context,
                            calls=turn["calls"],
                            phase=phase,
                            run_dir=run_dir,
                        )
                        turn["review"] = review
                    checks = program_checks + reviewer_checks
                    turn["evaluation"] = checks
                    phase = Phase.STAGE_TRANSITION
                    failed = [c for c in checks if not c["passed"]]
                    failed_gates = [c for c in failed if c["hard_gate"]]
                    turn["failed_hard_gates"] = [c["id"] for c in failed_gates]
                    if failed_gates:
                        if (
                            retry_count < stage.retry_limit
                            and turn_no < stage.max_turns
                        ):
                            # Only failed hard gates trigger a retry; non-gate failures are recorded and allowed to advance.
                            retry_count += 1
                            retry_text = retry_message_for(
                                self.public_language,
                                messages,
                                stage_start_message=stage_log["start_message"],
                                task=task,
                                stage=stage,
                            )
                            action = Action(
                                f"retry_{retry_count}",
                                UserMessage(type="fixed", text=retry_text),
                            )
                            turn["outcome"] = "retry"
                            continue
                        turn["retry_unavailable"] = (
                            "retry_limit"
                            if retry_count >= stage.retry_limit
                            else "max_turns"
                        )
                        turn["outcome"] = "failed"
                        stage_log["status"] = report["status"] = "failed"
                        report["reason"] = f"{stage.id} hard gate failed: " + ", ".join(
                            c["id"] for c in failed_gates
                        )
                        return
                    # Advancing does not imply 100% check pass; non-gate failures remain in the record for reward calculation.
                    turn["outcome"] = stage_log["status"] = self.stage_outcome(
                        stage, failed
                    )
                    # after_stage runs only after hard gates pass; placing it in finally would fire on failures and retries.
                    phase = Phase.AFTER_STAGE
                    self.run_hooks(
                        task,
                        phase,
                        run_dir,
                        report,
                        stage_id=stage.id,
                        turn=turn_no,
                        action_id=action.id,
                        workspace=workspace,
                    )
                    break
                except (Exception, KeyboardInterrupt) as exc:
                    turn["error"] = f"{type(exc).__name__}: {exc}"
                    # Match call_id within the current turn and phase so non-call errors are not misattributed to Worker.
                    failed_call = next(
                        (
                            c
                            for c in reversed(turn["calls"])
                            if c["role"] == phase.agent_role and c["phase"] == phase
                        ),
                        None,
                    )
                    turn["error_details"] = describe_error(
                        exc,
                        phase=phase,
                        call_id=failed_call["call_id"] if failed_call else None,
                    )
                    if getattr(exc, "__notes__", None):
                        turn["error_notes"] = list(exc.__notes__)
                    stage_log["status"] = report["status"] = "error"
                    report["reason"] = (
                        f"{stage.id} turn {turn_no} error: {turn['error']}"
                    )
                    if isinstance(exc, KeyboardInterrupt):
                        report["interrupted"] = True
                    return
                finally:
                    # Save report at every turn boundary (continue, break, return, or exception).
                    self.save_run_report(run_dir, report)
            else:
                stage_log["status"] = report["status"] = "budget_exhausted"
                report["reason"] = (
                    f"{stage.id} exhausted {stage.max_turns}-turn budget without completing"
                )
                return

        self.complete_run(task, report)
