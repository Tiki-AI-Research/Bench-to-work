"""Call boundary logging, protocol-error recording, evaluation scope attribution, and report persistence."""

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
import traceback

from .enums import CallStatus, ErrorCategory, Phase, Role
from .json_protocol import parse_model_json, validate_json
from .models import Agent, Call, ExternalExecutionError, OutputProtocolError, Reply
from .record_types import (
    CallRecord,
    ErrorDetails,
    EvaluationScope,
    RunRecord,
    StageRecord,
)
from .reports import render_report
from .storage import atomic_write_text, write_json


def invoke_agent(
    agent: Agent,
    call: Call,
    *,
    calls: list[CallRecord] | None = None,
    run_dir: Path | None = None,
    progress: Callable[[str], None] = print,
) -> Reply:
    """Record a single Call/Reply boundary; protocol validation, stage progression, and tool loops stay in the caller/adapter."""
    call.log_dir.mkdir(parents=True, exist_ok=True)
    record: CallRecord = {
        **call.identity,
        "role": call.role,
        "log_dir": str(call.log_dir.relative_to(run_dir))
        if run_dir
        else str(call.log_dir),
        "status": CallStatus.RUNNING,
    }
    if calls is not None:
        calls.append(record)
    # Serialize directly from Call so newly added fields (such as schema) are never omitted by manual field lists.
    request = asdict(call)
    request["workspace"] = str(call.workspace) if call.workspace is not None else None
    request["log_dir"] = str(call.log_dir)
    write_json(call.log_dir / "request.json", request)
    progress(f"  Calling {call.role} ...")
    try:
        reply = agent.run(call)
        record["trace_refs"] = list(reply.trace_refs)
        write_json(call.log_dir / "reply.json", asdict(reply))
    except (Exception, KeyboardInterrupt) as exc:
        record["status"] = CallStatus.ERROR
        record["error_details"] = describe_error(
            exc, role=call.role, phase=call.phase, call_id=call.call_id
        )
        try:
            write_json(call.log_dir / "error.json", record["error_details"])
        except OSError as log_error:
            exc.add_note(f"Failed to save call error log: {log_error}")
        raise
    record["status"] = CallStatus.RETURNED
    return reply


def parse_model_reply(
    reply: Reply, schema: dict, *, record: CallRecord, run_dir: Path
) -> dict:
    """Persist any JSON recovery trace before strict Schema validation; raw reply.json is never mutated."""
    data, recovery = parse_model_json(reply.text)
    if recovery is not None:
        path = Path(record["log_dir"]) / "json_recovery.json"
        write_json(run_dir / path, {"call_id": record["call_id"], **recovery})
        record["json_recovery"] = str(path)
    # Persist recovery details first so downstream Schema or check-ID failures remain inspectable.
    return validate_json(data, schema)


def record_protocol_error(
    exc: OutputProtocolError, *, record: CallRecord, run_dir: Path
) -> None:
    """Record protocol validation failure on a returned reply without misclassifying it as a transport error."""
    record["protocol_error"] = describe_error(
        exc, phase=record["phase"], role=record["role"], call_id=record["call_id"]
    )
    write_json(
        run_dir / record["log_dir"] / "protocol_error.json", record["protocol_error"]
    )


def describe_error(
    exc: BaseException,
    *,
    phase: Phase,
    role: Role | str | None = None,
    call_id: str | None = None,
) -> ErrorDetails:
    """Classify exceptions strictly by type rather than parsing message text or assigning training penalties."""
    if isinstance(exc, OutputProtocolError):
        category = ErrorCategory.OUTPUT_PROTOCOL
    elif isinstance(exc, (ExternalExecutionError, OSError, TimeoutError)):
        category = ErrorCategory.EXTERNAL_EXECUTION
    elif isinstance(exc, KeyboardInterrupt):
        category = ErrorCategory.INTERRUPTED
    else:
        category = ErrorCategory.UNKNOWN
    return {
        "role": role if role is not None else phase.agent_role,
        "phase": phase,
        "call_id": call_id,
        "category": category,
        "exception_type": type(exc).__name__,
        "message": str(exc),
        "notes": list(getattr(exc, "__notes__", [])),
        # Preserve stack frames and un-suppressed exception chains without capturing local variables.
        "traceback": "".join(traceback.format_exception(exc)),
    }


def evaluation_scope(stage_log: StageRecord, message_count: int) -> EvaluationScope:
    """Stage evidence spans the whole stage; attempt_worker_call_ids spans turns since the previous formal evaluation."""
    turns = stage_log["turns"]
    previous = next(
        (i for i in range(len(turns) - 2, -1, -1) if "evaluation_id" in turns[i]),
        -1,
    )

    def worker_ids(selected):
        return [
            c["call_id"]
            for t in selected
            for c in t["calls"]
            if c["role"] == Role.WORKER and c["status"] == CallStatus.RETURNED
        ]

    return {
        "worker_call_ids": worker_ids(turns),
        "attempt_worker_call_ids": worker_ids(turns[previous + 1 :]),
        # Half-open 0-based message index range [start, end) marking evidence scope, not reward credit assignment.
        "message_range": [stage_log["start_message"], message_count],
    }


def save_report(
    run_dir: Path,
    report: RunRecord,
    *,
    render: Callable[[RunRecord], str] = render_report,
) -> None:
    """Recompute derived retry counts and atomically save report.json and report.md."""
    # Derive retry counts from turns that actually started execution rather than scheduled-but-unrun retries.
    for stage in report["stages"]:
        stage["retry_count"] = max(
            (t.get("retry_count", 0) for t in stage["turns"]), default=0
        )
    report["retry_count"] = sum(stage["retry_count"] for stage in report["stages"])
    write_json(run_dir / "report.json", report)
    atomic_write_text(run_dir / "report.md", render(report))
