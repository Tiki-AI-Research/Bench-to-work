"""Static TypedDict definitions for run records; plain dicts at runtime with no extra validation or serialization layer."""

from typing import Any, Literal, NotRequired, TypedDict

from .enums import CallStatus, ErrorCategory, Phase, Role


class ErrorDetails(TypedDict):
    role: Role | str | None
    phase: Phase | str
    call_id: str | None
    category: ErrorCategory
    exception_type: str
    message: str
    notes: list[str]
    traceback: NotRequired[str]


class CallIdentity(TypedDict):
    run_id: str | None
    call_id: str | None
    stage_id: str | None
    turn_id: str | None
    turn: int | None
    retry_count: int
    phase: Phase | str
    protocol_retry_count: int
    protocol_retry_of: str | None


class CallRecord(CallIdentity):
    role: Role | str
    log_dir: str
    status: CallStatus
    trace_refs: NotRequired[list[str]]
    error_details: NotRequired[ErrorDetails]
    json_recovery: NotRequired[str]
    # Transport completion does not imply protocol validity; original protocol errors stay recorded after retry recovery.
    protocol_error: NotRequired[ErrorDetails]


class JsonRecovery(TypedDict):
    method: Literal["extracted", "repaired"]
    source_range: list[int]
    parser: str
    value: Any


class EvaluationScope(TypedDict):
    worker_call_ids: list[str]
    attempt_worker_call_ids: list[str]
    message_range: list[int]


class CheckResult(TypedDict):
    id: str
    passed: bool
    evidence: str
    # Raw evaluator output contains only check verdicts; Runner attaches task-defined hard_gate after ID validation.
    hard_gate: NotRequired[bool]


class VerificationResult(TypedDict):
    checks: list[CheckResult]
    observations: dict[str, Any]


class ReviewResult(TypedDict):
    reason: str
    criteria: list[CheckResult]


class AttachmentRecord(TypedDict):
    path: str
    kind: str
    saved_path: str


class TurnRecord(TypedDict):
    turn_id: str
    stage_id: str
    turn: int
    action_id: str
    calls: list[CallRecord]
    # Fields below are populated only once the corresponding step occurs; absence indicates unreached, not pass/fail.
    retry_count: NotRequired[int]
    released_attachments: NotRequired[list[str]]
    user_visible_files: NotRequired[list[str]]
    user_message_type: NotRequired[str]
    user: NotRequired[str]
    assistant: NotRequired[str]
    user_attachments: NotRequired[list[AttachmentRecord]]
    progress: NotRequired[dict[str, Any]]
    evaluation_id: NotRequired[str]
    evaluation_scope: NotRequired[EvaluationScope]
    reply_snapshot: NotRequired[str]
    snapshot: NotRequired[str]
    review_workspace: NotRequired[str]
    verification: NotRequired[VerificationResult]
    review: NotRequired[ReviewResult]
    evaluation: NotRequired[list[CheckResult]]
    failed_hard_gates: NotRequired[list[str]]
    outcome: NotRequired[str]
    retry_unavailable: NotRequired[str]
    error: NotRequired[str]
    error_details: NotRequired[ErrorDetails]
    error_notes: NotRequired[list[str]]


class StageRecord(TypedDict):
    id: str
    title: str
    status: str
    turns: list[TurnRecord]
    start_message: NotRequired[int]
    retry_count: NotRequired[int]


class HookRecord(TypedDict):
    id: str
    event: Phase | str
    stage_id: str | None
    turn: int | None
    action_id: str | None
    description: str
    status: str
    log_dir: str
    error: NotRequired[str]
    error_details: NotRequired[ErrorDetails]


class RunRecord(TypedDict):
    run_id: str
    task_id: str
    title: str
    mode: str
    status: str
    reason: str
    stages: list[StageRecord]
    hooks: list[HookRecord]
    retry_count: NotRequired[int]
    interrupted: NotRequired[bool]
    cleanup_error: NotRequired[str]
    cleanup_error_details: NotRequired[ErrorDetails]
    agent_cleanup_errors: NotRequired[list[ErrorDetails]]
