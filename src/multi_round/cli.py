"""CLI entry point: load task and adapter configuration, then hand off to Runner."""

import argparse
import json
import signal
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .agents import make_agent
from .agents.lifecycle import close_agent, finish_agent_cleanup
from .models import load_task
from .runner import Runner


@contextmanager
def _sigterm_as_interrupt():
    """Translate SIGTERM on the main thread into SystemExit so Runner/managed_process finally blocks kill child sessions."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    prev = signal.getsignal(signal.SIGTERM)

    def _on_sigterm(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, prev)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Multi-round stage task runner")
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    project = Path.cwd().resolve()
    task_file = args.task.resolve()
    task = load_task(task_file)
    design_file = task_file.parent / "verify" / "design.json"
    frozen_design = (
        json.loads(design_file.read_text(encoding="utf-8"))
        if design_file.is_file()
        else None
    )
    config = json.loads(args.config.read_text(encoding="utf-8"))
    valid_keys = {
        "mode",
        "user",
        "worker",
        "reviewer",
        "progress_reviewer",
        "reviewer_protocol_retry_limit",
        "verifier_timeout",
        "public_language",
    }
    if unknown := sorted(set(config) - valid_keys):
        raise ValueError(f"Unsupported configuration key(s): {', '.join(unknown)}")
    output = (
        args.output or Path("runs") / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    ).resolve()
    task = replace(
        task, verifier=[arg.replace("{project}", str(project)) for arg in task.verifier]
    )
    report, cleanup_errors = None, []
    with _sigterm_as_interrupt():
        try:
            with ExitStack() as stack:
                agents = {}
                for role in ("user", "worker", "reviewer", "progress_reviewer"):
                    if role == "progress_reviewer" and role not in config:
                        continue
                    agent = agents[role] = make_agent(config[role], project)
                    close = getattr(agent, "close", None)
                    if close:
                        stack.callback(close_agent, close, role, cleanup_errors)
                report = Runner(
                    **agents,
                    reviewer_protocol_retry_limit=config.get(
                        "reviewer_protocol_retry_limit", 1
                    ),
                    verifier_timeout=config.get("verifier_timeout", 60),
                    public_language=config.get("public_language"),
                ).run(
                    task,
                    output,
                    mode=config.get("mode", "live"),
                    run_config=config,
                    frozen_design=frozen_design,
                )
        finally:
            finish_agent_cleanup(cleanup_errors, report, output)
    print(
        f"\nStatus: {report['status']}\nReport: {output.resolve() / 'report.md'}",
        flush=True,
    )
    return 0 if report["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
