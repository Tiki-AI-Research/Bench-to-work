"""CLI instantiates configured agents directly and closes resources on both success and failure."""

import json
import sys
from contextlib import nullcontext
from types import ModuleType

import pytest

from multi_round.cli import main
from multi_round.models import Reply
from multi_round.runner import Runner


def test_unknown_config_keys_fail_fast_before_starting_agents(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text('{"unknown_role": {}}', encoding="utf-8")
    monkeypatch.setattr("multi_round.cli.load_task", lambda path: None)

    def unexpected(*args):
        pytest.fail("Unknown config keys must fail before starting any Agent")

    monkeypatch.setattr("multi_round.cli.make_agent", unexpected)
    with pytest.raises(ValueError, match="Unsupported configuration key"):
        main(["--task", str(tmp_path / "task.json"), "--config", str(config_path)])


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--task", "some_task.json"],
        ["--config", "some_config.json"],
    ],
)
def test_cli_requires_explicit_task_and_config_arguments(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--task" in err or "--config" in err


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "worker_run",
        "worker_init",
        "reviewer_close",
        "report_write",
    ],
)
@pytest.mark.parametrize("separate_progress", [False, True])
def test_cli_uses_configured_agents_and_closes_resources(
    failure, separate_progress, monkeypatch, tmp_path
):
    events = []
    module = ModuleType("fixture_plugins")

    class Agent:
        def __init__(self, name):
            self.name = name

        def run(self, call):
            # Config is persisted before the first agent call without waiting for Runner to return.
            saved_config = json.loads(
                (tmp_path / "run/config.json").read_text(encoding="utf-8")
            )
            assert saved_config == config
            if call.role == "worker":
                if failure == "worker_run":
                    raise RuntimeError("fixture failure: worker run")
                return Reply("Please confirm scope")
            if call.role == "user":
                return Reply("Please process the data")
            if "decision" in call.schema["properties"]:
                assert self.name == (
                    "progress_reviewer" if separate_progress else "reviewer"
                )
                return Reply(
                    '{"reason":"Asked question","decision":"review_submission","continuation_id":null}'
                )
            assert self.name == "reviewer"
            return Reply(
                '{"reason":"Clarified","criteria":[{"id":"ask","passed":true,"evidence":"Asked for scope"}]}'
            )

        def close(self):
            events.append("close_" + self.name)
            if failure == "reviewer_close" and self.name == "reviewer":
                raise RuntimeError("fixture failure: reviewer close")

    def create_agent(project, *, name):
        if failure == "worker_init" and name == "worker":
            raise RuntimeError("fixture failure: worker init")
        return Agent(name)

    module.create_agent = create_agent
    monkeypatch.setitem(sys.modules, "fixture_plugins", module)
    task = {
        "id": "independent",
        "title": "Generic Wiring",
        "persona": "User",
        "background": "Background",
        "stages": [
            {
                "id": "ask",
                "title": "Clarify",
                "initial": {
                    "id": "ask",
                    "message": {"type": "context", "instruction": "Ask to process data"},
                },
                "checkpoint": "First question",
                "needs_workspace": False,
                "verifier_checks": [],
                "criteria": [{"id": "ask", "description": "Asks about scope"}],
            }
        ],
    }
    (tmp_path / "task.json").write_text(json.dumps(task), encoding="utf-8")
    roles = ["user", "worker", "reviewer"]
    if separate_progress:
        roles.append("progress_reviewer")
    config = {
        role: {
            "type": "python",
            "factory": "fixture_plugins:create_agent",
            "name": role,
        }
        for role in roles
    }
    config["verifier_timeout"] = 7

    def configured_runner(**kwargs):
        assert kwargs["verifier_timeout"] == config["verifier_timeout"]
        return Runner(**kwargs)

    monkeypatch.setattr("multi_round.cli.Runner", configured_runner)
    (tmp_path / "config.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    argv = ["--task", "task.json", "--config", "config.json", "--output", "run"]
    if failure == "report_write":

        def fail_save(*args):
            raise RuntimeError("fixture failure: report write")

        monkeypatch.setattr("multi_round.runner.save_report", fail_save)
    expected = (
        nullcontext()
        if failure in {None, "worker_run"}
        else pytest.raises(RuntimeError, match="fixture failure")
    )
    with expected:
        assert main(argv) == int(failure == "worker_run")

    if failure == "worker_init":
        assert events == ["close_user"]
        return
    # Active config remains persisted even when report saving or resource cleanup fails.
    assert (
        json.loads((tmp_path / "run/config.json").read_text(encoding="utf-8")) == config
    )
    assert events == ["close_" + role for role in reversed(roles)]
    if failure != "report_write":
        report = json.loads((tmp_path / "run/report.json").read_text(encoding="utf-8"))
        assert report["status"] == (
            "error" if failure in {"worker_run", "reviewer_close"} else "completed"
        )
        if failure == "reviewer_close":
            assert report["agent_cleanup_errors"][0]["phase"] == "agent_cleanup"
            assert report["agent_cleanup_errors"][0]["role"] == "reviewer"
            assert report["stages"][0]["turns"][0]["evaluation"][0]["passed"]
            assert "reviewer close" in (tmp_path / "run/report.md").read_text(
                encoding="utf-8"
            )


def test_unknown_task_fields_are_rejected_before_agents_start(tmp_path):
    from multi_round.models import load_task

    path = tmp_path / "bad-task.json"
    path.write_text('{"unsupported_field":"value"}', encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported task field"):
        load_task(path)


def test_cli_sigterm_cleans_up_detached_worker_session_before_exit(tmp_path):
    import os
    import signal
    import subprocess
    import time
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    ready_file = tmp_path / "worker_started.flag"
    late_file = tmp_path / "late_write_after_sigterm.txt"
    worker_script = tmp_path / "delayed_worker.py"
    worker_script.write_text(
        f"""import json, pathlib, sys, time
_ = json.load(sys.stdin)
pathlib.Path({str(ready_file)!r}).write_text("ready", encoding="utf-8")
time.sleep(1.2)
pathlib.Path({str(late_file)!r}).write_text("escaped", encoding="utf-8")
print(json.dumps({{"text": "done"}}))
""",
        encoding="utf-8",
    )
    demo_cfg = json.loads((repo_root / "tests" / "fixtures" / "excel_revision" / "demo.json").read_text(encoding="utf-8"))
    demo_cfg["worker"] = {"type": "command", "command": [sys.executable, "-B", str(worker_script)], "timeout": 30}
    cfg_path = tmp_path / "sigterm_cfg.json"
    cfg_path.write_text(json.dumps(demo_cfg), encoding="utf-8")

    env = {**os.environ, "PYTHONPATH": str(repo_root / "src"), "PYTHONUTF8": "1"}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "multi_round.cli",
            "--task",
            str(repo_root / "tests" / "fixtures" / "excel_revision" / "task.json"),
            "--config",
            str(cfg_path),
            "--output",
            str(tmp_path / "out"),
        ],
        cwd=str(repo_root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    deadline = time.monotonic() + 10.0
    while not ready_file.is_file():
        assert time.monotonic() < deadline and proc.poll() is None
        time.sleep(0.02)

    os.killpg(proc.pid, signal.SIGTERM)
    rc = proc.wait(timeout=5.0)
    assert rc != 0
    time.sleep(1.5)
    assert not late_file.exists(), "Detached Worker session kept writing after CLI handled SIGTERM"
