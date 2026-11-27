"""Human-readable Markdown report rendering; pure formatting with no state mutation, scoring, or file I/O."""

from .record_types import RunRecord, TurnRecord

LABELS = {
    "pending": "Pending",
    "running": "Running",
    "passed": "Passed",
    "advanced_with_errors": "Non-gate checks failed; advanced",
    "failed": "Hard gate failed",
    "error": "Execution / evaluation error",
    "budget_exhausted": "Turn budget exhausted",
    "completed": "Completed",
}


def render_report(report: RunRecord, *, labels: dict[str, str] = LABELS) -> str:
    """Render an aggregated RunRecord to Markdown without mutating records or touching disk."""
    lines = [
        f"# {report['title']}: Run Report",
        "",
        f"Mode: {report['mode']}",
        "",
        f"Status: {labels.get(report['status'], report['status'])}",
        "",
        f"Reason: {report['reason']}",
        "",
        f"Retries initiated: {report['retry_count']}",
        "",
    ]
    if "run_id" in report:
        lines += [f"Run ID: `{report['run_id']}`", ""]
    for stage in report["stages"]:
        lines += [
            f"## {stage['title']} · {labels[stage['status']]}",
            "",
            f"Stage retries initiated: {stage['retry_count']}",
            "",
        ]
        if not stage["turns"]:
            lines += ["Stage did not run; not evaluated.", ""]
        for turn in stage["turns"]:
            lines.extend(_render_turn(turn))
    if report.get("hooks"):
        lines += [
            "## Environment Events",
            "",
            "Triggered by task hooks, not Worker actions.",
            "",
        ]
        for event in report["hooks"]:
            position = (
                f"{event['stage_id']} / turn {event['turn']}"
                if event["stage_id"]
                else "after_run"
            )
            lines += [
                f"- {position} · {event['event']} · {event['id']} · {event['status']}: "
                f"{event['description']} ([logs](<{event['log_dir']}/>))"
            ]
            if "error" in event:
                lines.append(f"  Error: {event['error']}")
        lines.append("")
    if report.get("cleanup_error"):
        lines += [f"Post-run cleanup error: {report['cleanup_error']}", ""]
    for error in report.get("agent_cleanup_errors", []):
        lines += [
            f"Agent cleanup error ({error['role']}): {error['exception_type']}: {error['message']}",
            "",
        ]
    return "\n".join(lines)


def _render_turn(turn: TurnRecord) -> list[str]:
    """Render messages, evaluation results, and retry/error details for a single turn."""
    lines = [f"### Turn {turn['turn']}", ""]
    if "turn_id" in turn:
        lines += [f"Turn ID: `{turn['turn_id']}`", ""]
    for call in turn.get("calls", []):
        lines += [
            f"- {call['role']} / {call['phase']}: `{call['call_id']}` "
            f"([call logs](<{call['log_dir']}/>), {call['status']})"
        ]
        for ref in call.get("trace_refs", []):
            lines.append(f"  Trace ref: `{ref}`")
        if call.get("protocol_retry_count", 0):
            lines.append(
                f"  Protocol retry #{call['protocol_retry_count']}; "
                f"original call: `{call['protocol_retry_of']}`"
            )
        if "protocol_error" in call:
            lines.append(
                f"  Protocol error: {call['protocol_error']['message']} "
                f"([details](<{call['log_dir']}/protocol_error.json>))"
            )
        if "json_recovery" in call:
            lines.append(f"  JSON recovery: [recovery record](<{call['json_recovery']}>)")
    if turn.get("calls"):
        lines.append("")
    lines += ["**User**", "", turn.get("user", "Not returned"), ""]
    for attachment in turn.get("user_attachments", []):
        prefix = "!" if attachment["kind"] == "image" else ""
        lines += [
            f"{prefix}[{attachment['path']}](<{attachment['saved_path']}>)",
            "",
        ]
    lines += ["**Worker**", "", turn.get("assistant", "Not returned"), ""]
    if "progress" in turn:
        progress = turn["progress"]
        lines += [
            f"**Progress decision: {progress['decision']}** — {progress['reason']}",
            "",
        ]
    if "retry_count" in turn:
        lines += [
            f"Retries initiated before this turn: {turn['retry_count']} (0 means initial attempt)",
            "",
        ]
    if "snapshot" in turn:
        lines += [f"Submission snapshot: [{turn['snapshot']}]({turn['snapshot']}/)", ""]
    if "review_workspace" in turn:
        history = f"{turn['review_workspace']}/evaluation/conversation.jsonl"
        lines += [f"Evaluation conversation: [full public conversation]({history})", ""]
    if "review" in turn:
        lines += [f"**Semantic evaluation** — {turn['review']['reason']}", ""]
    if "evaluation_id" in turn:
        scope = turn["evaluation_scope"]
        lines += [
            f"Evaluation ID: `{turn['evaluation_id']}`; "
            f"{len(scope['worker_call_ids'])} Worker call(s) in stage, "
            f"{len(scope['attempt_worker_call_ids'])} in this attempt.",
            "",
        ]
    checks = turn.get("evaluation")
    if checks is None and "verification" in turn:
        # Preserve completed programmatic check results in the human report even if Reviewer subsequently failed.
        checks = turn["verification"]["checks"]
        lines += [
            "**Programmatic checks completed; full stage evaluation did not finish.**",
            "",
        ]
    if checks:
        lines += ["| Check | Type | Result | Evidence |", "|---|---|---|---|"]
        for c in checks:
            evidence = c["evidence"].replace("|", "\\|").replace("\n", " ")
            kind = "Programmatic"
            if "hard_gate" in c:
                kind = "Hard gate" if c["hard_gate"] else "Non-gate"
            lines.append(
                f"| {c['id']} | {kind} | "
                f"{'Pass' if c['passed'] else 'Fail'} | {evidence} |"
            )
        lines.append("")
    if turn.get("outcome") == "retry":
        lines += [
            "Hard gate failed; scheduled an in-stage retry (actual execution is tracked by retry_count; all attempts are preserved).",
            "",
        ]
    if turn.get("outcome") == "advanced_with_errors":
        lines += [
            "Hard gates passed; non-gate failures do not trigger a retry—evaluation recorded and stage advanced.",
            "",
        ]
    if "retry_unavailable" in turn:
        reason = (
            "retry_limit reached (or configured as 0)"
            if turn["retry_unavailable"] == "retry_limit"
            else "stage max_turns budget reached"
        )
        lines += [f"Retry unavailable: {reason}.", ""]
    if "error" in turn:
        lines += [f"Error: {turn['error']}", ""]
        if "error_details" in turn:
            error = turn["error_details"]
            detail = f"Error phase: {error['phase']}; category: {error['category']}"
            if error["role"] is not None:
                detail += f"; agent role: {error['role']}"
            if error["call_id"] is not None:
                detail += f"; call ID: `{error['call_id']}`"
            lines += [detail, ""]
        for note in turn.get("error_notes", []):
            lines += [f"Note: {note}", ""]
    return lines
