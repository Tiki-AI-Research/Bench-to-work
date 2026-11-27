"""UTF-8 subprocess execution and process-tree lifecycle; agnostic to models, tasks, and scoring."""

from collections.abc import Iterator
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
from typing import TextIO

from .models import ExternalExecutionError


def protocol_env() -> dict[str, str]:
    """Enforce UTF-8 stdio on Python child processes regardless of host locale."""
    return {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def report_cleanup_errors(
    errors: list[str],
    primary_error: BaseException | None,
    path: Path,
    *,
    error_type: type[Exception] = RuntimeError,
) -> None:
    """Preserve the primary execution exception while recording cleanup failures; fail the call on cleanup error only when execution succeeded."""
    if not errors:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"errors": errors}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError as exc:
        errors = [*errors, f"Failed to write cleanup log: {exc}"]
    detail = "Cleanup failed: " + "; ".join(errors)
    if primary_error is not None:
        primary_error.add_note(detail)
    else:
        raise error_type(detail)


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Kill the entire child process group/tree so timed-out commands leave no background writers."""
    if os.name == "nt":
        if process.poll() is None:
            # Popen.terminate()/kill() only stops the parent on Windows; taskkill /T /F terminates the tree.
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if result.returncode and process.poll() is None:
                raise RuntimeError(
                    f"taskkill failed to terminate process tree (exit code {result.returncode})"
                )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # Process group already exited.
    process.wait(timeout=5)


@contextmanager
def managed_process(
    argv: list[str],
    *,
    cwd: Path,
    log_dir: Path,
    stdin: int | TextIO | None,
    stdout: int | TextIO | None,
    stderr: int | TextIO | None,
) -> Iterator[subprocess.Popen[str]]:
    """Spawn a process in its own group/session and guarantee tree + pipe cleanup on exit, error, or cancellation."""
    options = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=True,
        encoding="utf-8",
        env=protocol_env(),
        **options,
    )
    primary_error = None
    try:
        yield process
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        errors = []
        try:
            terminate_process_tree(process)
        except Exception as exc:
            errors.append(f"process_tree: {type(exc).__name__}: {exc}")
        # Close Popen-owned pipes here; caller-opened log file handles are closed by their own context managers.
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except Exception as exc:
                    errors.append(f"pipe: {type(exc).__name__}: {exc}")
        report_cleanup_errors(errors, primary_error, log_dir / "cleanup.json")


def run_process(
    argv: list[str], *, cwd: Path, stdin: str, timeout: int, log_dir: Path
) -> str:
    """Execute argv without a shell; terminate the full process tree on timeout so no background child keeps mutating files."""
    log_dir.mkdir(parents=True, exist_ok=True)
    with (
        (log_dir / "stdout.log").open("w", encoding="utf-8") as out,
        (log_dir / "stderr.log").open("w", encoding="utf-8") as err,
        managed_process(
            argv,
            cwd=cwd,
            log_dir=log_dir,
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=err,
        ) as proc,
    ):
        try:
            proc.communicate(stdin, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ExternalExecutionError(
                f"External command timed out; see {log_dir}"
            ) from None
        if proc.returncode:
            raise ExternalExecutionError(
                f"External command exited with code {proc.returncode}; see {log_dir}"
            )
    return (log_dir / "stdout.log").read_text(encoding="utf-8")
