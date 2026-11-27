"""ExitStack agent teardown helpers; cleanup errors are recorded separately without masking primary exceptions."""

import sys
from collections.abc import Callable
from pathlib import Path

from ..enums import Phase, Role
from ..record_types import RunRecord
from ..records import describe_error, save_report


def close_agent(
    close: Callable[[], None], role: str, errors: list[tuple[str, Exception]]
) -> None:
    """ExitStack callback: one agent failing to close must not block releasing remaining agents."""
    try:
        close()
    except Exception as exc:
        errors.append((role, exc))


def finish_agent_cleanup(
    errors: list[tuple[str, Exception]], report: RunRecord | None, run_dir: Path
) -> None:
    """Attach cleanup failures to the current run report; never create or overwrite a report if run() produced none."""
    if not errors:
        return
    primary = sys.exception()
    details = [
        f"{role} agent cleanup failed: {type(exc).__name__}: {exc}"
        for role, exc in errors
    ]
    first = errors[0][1]
    if report is not None:
        report["agent_cleanup_errors"] = [
            describe_error(
                exc,
                phase=Phase.AGENT_CLEANUP,
                role=Role.REVIEWER if role == "progress_reviewer" else role,
            )
            for role, exc in errors
        ]
        # Mark the overall run as error on adapter teardown failure while preserving completed stage evaluations.
        if report["status"] != "error":
            report["status"] = "error"
            report["reason"] += "; " + "; ".join(details)
        try:
            save_report(run_dir, report)
        except Exception as exc:
            first.add_note(
                f"Failed to save agent cleanup report: {type(exc).__name__}: {exc}"
            )
    if primary is not None:
        for detail in details:
            primary.add_note(detail)
        for note in getattr(first, "__notes__", []):
            primary.add_note(note)
    else:
        for detail in details[1:]:
            first.add_note(detail)
        raise first
