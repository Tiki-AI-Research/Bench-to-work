"""Process-tree lifecycle and UTF-8 stdio tests across POSIX and Windows."""

import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from multi_round import processes
from multi_round.models import AdapterError
from multi_round.agents import make_agent
from multi_round.processes import run_process
from multi_round.models import Call


def test_protocol_is_utf8_even_when_python_environment_requests_another_encoding(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PYTHONUTF8", "0")
    monkeypatch.setenv("PYTHONIOENCODING", "ascii")
    message = "café, €, 🙂"
    result = run_process(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        cwd=tmp_path,
        stdin=message,
        timeout=10,
        log_dir=tmp_path / "logs",
    )
    assert result == message
    assert (tmp_path / "logs/stdout.log").read_bytes() == message.encode("utf-8")
    assert os.environ["PYTHONIOENCODING"] == "ascii"  # Only the child process environment is overridden.


def test_timeout_terminates_child_and_grandchild(tmp_path):
    script = tmp_path / "tree.py"
    script.write_text(
        """import subprocess, sys, time
from pathlib import Path
root, level = Path(sys.argv[1]), int(sys.argv[2])
child = None
if level < 2:
    child = subprocess.Popen([sys.executable, __file__, str(root), str(level + 1)])
(root / f"ready-{level}").write_text("ready", encoding="utf-8")
while not (root / "release").exists():
    time.sleep(0.02)
time.sleep(1)
(root / f"survived-{level}").write_text("alive", encoding="utf-8")
if child:
    child.wait()
""",
        encoding="utf-8",
    )
    with pytest.raises(subprocess.TimeoutExpired):
        with processes.managed_process(
            [sys.executable, str(script), str(tmp_path), "0"],
            cwd=tmp_path,
            log_dir=tmp_path / "logs",
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as process:
            deadline = time.monotonic() + 10
            while len(list(tmp_path.glob("ready-*"))) != 3:
                assert time.monotonic() < deadline, "Process tree did not start in time"
                time.sleep(0.02)
            process.communicate(timeout=0.1)
    assert process.poll() is not None
    (tmp_path / "release").touch()
    time.sleep(1.3)
    assert not list(tmp_path.glob("survived-*"))


@pytest.mark.parametrize(
    "code, expected",
    [
        ("import time; time.sleep(10)", "timed out"),
        ("import sys; sys.exit(7)", "exited with code 7"),
    ],
)
def test_cleanup_failure_does_not_hide_execution_error(
    tmp_path, monkeypatch, code, expected
):
    terminate = processes.terminate_process_tree

    def fail_after_termination(process):
        terminate(process)
        raise OSError("fixture cleanup failure")

    monkeypatch.setattr(processes, "terminate_process_tree", fail_after_termination)
    with pytest.raises(AdapterError, match=expected) as error:
        run_process(
            [sys.executable, "-c", code],
            cwd=tmp_path,
            stdin="",
            timeout=0.1 if expected == "timed out" else 10,
            log_dir=tmp_path / "logs",
        )
    assert "fixture cleanup failure" in error.value.__notes__[0]
    assert json.loads((tmp_path / "logs/cleanup.json").read_text(encoding="utf-8"))[
        "errors"
    ]


def test_cleanup_failure_after_success_is_reported(tmp_path, monkeypatch):
    terminate = processes.terminate_process_tree

    def fail_after_termination(process):
        terminate(process)
        raise OSError("fixture cleanup failure")

    monkeypatch.setattr(processes, "terminate_process_tree", fail_after_termination)
    try:
        raise ValueError("previously handled exception")
    except ValueError:
        # Subprocess calls inside an except handler must not swallow a new cleanup failure.
        with pytest.raises(RuntimeError, match="fixture cleanup failure"):
            run_process(
                [sys.executable, "-c", "print('done')"],
                cwd=tmp_path,
                stdin="",
                timeout=10,
                log_dir=tmp_path / "logs",
            )
    assert (tmp_path / "logs/stdout.log").read_text(encoding="utf-8").strip() == "done"


def test_cancellation_is_not_converted_to_timeout(tmp_path):
    with pytest.raises(KeyboardInterrupt):
        with processes.managed_process(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            log_dir=tmp_path / "logs",
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as process:
            raise KeyboardInterrupt()
    assert process.poll() is not None


def test_windows_terminates_tree_instead_of_only_parent(monkeypatch):
    calls = []
    process = SimpleNamespace(
        pid=123, poll=lambda: None, wait=lambda **kwargs: calls.append("wait")
    )
    monkeypatch.setattr(processes, "os", SimpleNamespace(name="nt"))

    def taskkill(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(processes.subprocess, "run", taskkill)
    processes.terminate_process_tree(process)
    assert calls == [["taskkill", "/PID", "123", "/T", "/F"], "wait"]


def test_command_paths_use_current_python_and_preserve_non_path_arguments(tmp_path):
    project = tmp_path / "project café with spaces"
    project.mkdir()
    script = project / "echo.py"
    script.write_text(
        "import json, sys; request=json.load(sys.stdin); "
        "print(json.dumps({'text': request['prompt'] + sys.argv[1]}, ensure_ascii=False))",
        encoding="utf-8",
    )
    agent = make_agent(
        {
            "type": "command",
            "command": ["{python}", "{project}/echo.py", "https://example.invalid/a/b"],
        },
        project,
    )
    assert agent.command[:2] == [sys.executable, str(script)]
    reply = agent.run(Call("worker", "café🙂", project, tmp_path / "logs"))
    assert reply.text == "café🙂https://example.invalid/a/b"
