"""Verify HarborWorkspace file and command protocols using real files and deterministic command doubles."""

import asyncio
import json
import shutil
import time
from pathlib import Path, PurePosixPath
from threading import Event
from types import SimpleNamespace

import pytest
from multi_round_harbor.agent import MainAgent
from multi_round_harbor.bridge import EnvironmentBridge, GuardedAgent
from multi_round_harbor.verifier import final_check_fraction
from multi_round_harbor.worker import HarborWorker
from multi_round_harbor.workspace import HarborWorkspace

from multi_round.models import Call, ExternalExecutionError


class FilesystemEnvironment:
    def __init__(self):
        self.calls = []

    async def exec(self, command, cwd=None, timeout_sec=None):
        self.calls.append("exec")
        process = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout_sec)
        return SimpleNamespace(
            return_code=process.returncode,
            stdout=stdout.decode(),
            stderr=stderr.decode(),
        )

    async def upload_file(self, source, target):
        self.calls.append("upload_file")
        shutil.copy2(source, target)

    async def download_file(self, source, target):
        self.calls.append("download_file")
        shutil.copy2(source, target)

    async def download_dir(self, source, target):
        self.calls.append("download_dir")
        shutil.copytree(source, target)


def run_workspace(tmp_path, operation):
    async def run():
        root = tmp_path / "remote"
        root.mkdir()
        bridge = EnvironmentBridge()
        workspace = HarborWorkspace(FilesystemEnvironment(), bridge, str(root))
        await asyncio.to_thread(operation, workspace, bridge)

    asyncio.run(run())


def test_publish_current_hook_version_and_new_snapshots(tmp_path):
    source = tmp_path / "input.txt"
    source.write_text("before")

    def check(workspace, bridge):
        workspace.publish(source, "input.txt")
        with pytest.raises(ExternalExecutionError):
            workspace.publish(source, "input.txt")
        workspace.execute(
            [
                "python3",
                "-c",
                "from pathlib import Path; Path('input.txt').write_text('after')",
            ],
            stdin="",
            timeout=5,
            log_dir=tmp_path / "hook",
        )
        sent = tmp_path / "sent.txt"
        workspace.fetch_file("input.txt", sent)
        assert sent.read_text() == "after"
        assert set(
            workspace.collect(["input.txt", "missing.txt"], tmp_path / "first")
        ) == {"input.txt"}
        Path(workspace.root / "input.txt").unlink()
        assert workspace.collect(["input.txt"], tmp_path / "second") == {}
        assert (tmp_path / "first/input.txt").read_text() == "after"
        assert not (tmp_path / "second/input.txt").exists()

    run_workspace(tmp_path, check)


def test_file_management_does_not_transfer_command_io(tmp_path):
    """File transfers send only task files without redundant empty-stdin or dual-log roundtrips."""
    source = tmp_path / "source.txt"
    source.write_text("material", encoding="utf-8")

    def check(workspace, bridge):
        calls = workspace.environment.calls
        workspace.publish(source, "input.txt")
        assert calls.count("upload_file") == 1
        assert "download_file" not in calls
        assert len(calls) <= 2
        calls.clear()
        workspace.collect(["input.txt"], tmp_path / "submission")
        assert calls.count("download_dir") == 1
        assert "upload_file" not in calls and "download_file" not in calls
        assert len(calls) <= 3
        assert (tmp_path / "submission/input.txt").read_text() == "material"

    run_workspace(tmp_path, check)


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_failed_submission_download_still_cleans_temporary_directory(
    tmp_path, cleanup_fails
):
    def check(workspace, bridge):
        remote_directory = None
        original_exec = workspace.environment.exec

        async def fail_download(source, target):
            nonlocal remote_directory
            remote_directory = Path(source)
            raise OSError("download failed")

        async def cleanup(command, **kwargs):
            result = await original_exec(command, **kwargs)
            if cleanup_fails and " rm -rf " in command:
                result.return_code = 9
            return result

        workspace.environment.download_dir = fail_download
        workspace.environment.exec = cleanup
        with pytest.raises(ExternalExecutionError, match="download failed") as caught:
            workspace.collect(["."], tmp_path / "submission")
        assert not remote_directory.exists()
        if cleanup_fails:
            assert any(
                "Remote submission temp dir cleanup failed" in note
                for note in caught.value.__notes__
            )

    run_workspace(tmp_path, check)


@pytest.mark.parametrize("name", ["../hidden", "/outside"])
def test_reject_outside_paths(tmp_path, name):
    def check(workspace, bridge):
        with pytest.raises(ValueError):
            workspace.collect([name], tmp_path / "submission")

    run_workspace(tmp_path, check)


def test_reject_symlink_and_transfer_failure(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("hidden")

    def check(workspace, bridge):
        Path(workspace.root / "link").symlink_to(secret)
        with pytest.raises(ExternalExecutionError):
            workspace.collect(["."], tmp_path / "submission")
        with pytest.raises(ExternalExecutionError):
            workspace.fetch_file("link", tmp_path / "leak")
        assert not (tmp_path / "leak").exists()

    run_workspace(tmp_path, check)


def test_worker_session_identity_and_private_log_path(tmp_path):
    def check(workspace, bridge):
        code = """import json,sys
c=json.load(sys.stdin)
assert 'log_dir' not in c
assert c['stage_id']=='s2' and c['retry_count']==1
print(json.dumps({'text':'done','session_id':c['session_id'],'trace_refs':['native:123']}))
"""
        worker = HarborWorker(workspace, ["python3", "-c", code])
        call = Call(
            "worker",
            "repair",
            workspace.root,
            tmp_path / "worker",
            session_id="same",
            stage_id="s2",
            retry_count=1,
        )
        reply = worker.run(call)
        assert reply.session_id == "same" and reply.trace_refs == ["native:123"]
        assert isinstance(call.workspace, PurePosixPath)
        assert json.loads((call.log_dir / "stdout.log").read_text())["text"] == "done"

    run_workspace(tmp_path, check)


def test_failed_command_timeout_and_cancel_prevent_next_write(tmp_path):
    def check(workspace, bridge):
        for command in (["sh", "-c", "exit 4"], ["sh", "-c", "sleep 1; touch late"]):
            with pytest.raises(ExternalExecutionError):
                workspace.execute(
                    command, stdin="", timeout=0.05, log_dir=tmp_path / "logs"
                )
        assert not Path(workspace.root / "late").exists()
        bridge.cancelled.set()
        with pytest.raises(ExternalExecutionError, match="cancelled"):
            workspace.execute(
                ["touch", "after-cancel"],
                stdin="",
                timeout=1,
                log_dir=tmp_path / "cancel",
            )
        assert not Path(workspace.root / "after-cancel").exists()
        guard = GuardedAgent(
            SimpleNamespace(run=lambda _: pytest.fail("Should not be invoked")), bridge
        )
        with pytest.raises(ExternalExecutionError):
            guard.run(None)

    run_workspace(tmp_path, check)


def test_reward_counts_unrun_and_final_attempt():
    task = {"stages": [{"criteria": [1], "verifier_checks": [1]}] * 2}
    report = {
        "stages": [
            {
                "turns": [
                    {"evaluation": [{"passed": False}, {"passed": False}]},
                    {"evaluation": [{"passed": True}, {"passed": False}]},
                ]
            },
            {"turns": []},
        ]
    }
    assert final_check_fraction(report, task) == {"reward": 0.25}


def test_example_entrypoints_share_trusted_report_directory(tmp_path):
    """Instantiate MainAgent and ReportVerifier via Harbor factories to catch import-path drift."""
    from harbor.agents.factory import AgentFactory
    from harbor.models.job.config import JobConfig
    from harbor.models.trial.paths import TrialPaths
    from harbor.verifier.factory import VerifierFactory

    extension = Path(__file__).resolve().parents[1]
    config = JobConfig.model_validate_json(
        (extension / "tests/fixtures/harbor/job.json").read_text(encoding="utf-8")
    )
    agent_config = config.agents[0]
    for key in ("project", "task_path", "config_path"):
        agent_config.kwargs[key] = str((extension / agent_config.kwargs[key]).resolve())
    paths = TrialPaths(tmp_path / "trial")
    paths.mkdir()
    agent = AgentFactory.create_agent_from_config(
        agent_config, logs_dir=paths.agent_dir
    )
    assert isinstance(agent, MainAgent)
    assert agent.run_dir == paths.trial_dir / "rollout"

    agent.run_dir.mkdir()
    records = {
        "report": {
            "run_id": "example",
            "status": "completed",
            "stages": [{"turns": [{"evaluation": [{"passed": True}]}]}],
        },
        "harbor": {"run_id": "example"},
        "task": {"stages": [{"criteria": [1], "verifier_checks": []}]},
    }
    for name, data in records.items():
        (agent.run_dir / f"{name}.json").write_text(json.dumps(data), encoding="utf-8")
    verifier = VerifierFactory.create_verifier_from_config(
        config.verifier, task=None, trial_paths=paths, environment=None
    )
    assert asyncio.run(verifier.verify()).rewards == {"reward": 1.0}


def test_cleanup_failure_does_not_replace_command_failure(tmp_path):
    def check(workspace, bridge):
        original = workspace.environment.exec

        async def fail_cleanup(command, **kwargs):
            result = await original(command, **kwargs)
            if command.startswith("rm -f /tmp/harbor-command-"):
                result.return_code = 9
            return result

        workspace.environment.exec = fail_cleanup
        with pytest.raises(ExternalExecutionError, match="exited with code 7"):
            workspace.execute(
                ["sh", "-c", "echo primary >&2; exit 7"],
                stdin="",
                timeout=5,
                log_dir=tmp_path / "logs",
            )
        error = (tmp_path / "logs/stderr.log").read_text()
        assert "primary" in error and "Cleanup also failed" in error

    run_workspace(tmp_path, check)


def test_trial_cancellation_waits_for_current_call_and_saves_report(
    tmp_path, monkeypatch
):
    task = tmp_path / "task.json"
    task.write_text(
        json.dumps(
            {
                "id": "cancel",
                "title": "Cancel",
                "persona": "User",
                "background": "Test",
                "stages": [
                    {
                        "id": "s1",
                        "title": "Single Call",
                        "initial": {
                            "id": "start",
                            "message": {"type": "fixed", "text": "Start"},
                        },
                        "checkpoint": "Submit",
                        "needs_workspace": False,
                        "verifier_checks": [],
                        "criteria": [{"id": "done", "description": "Completed"}],
                    }
                ],
            }
        )
    )
    root = tmp_path / "remote"
    root.mkdir()
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "workspace": {"root": str(root)},
                "worker": {"command": ["unused"]},
                "user": {},
                "reviewer": {},
            }
        )
    )
    from multi_round.models import Reply

    started, finished = Event(), Event()

    def worker(_self, call):
        started.set()
        time.sleep(0.1)
        finished.set()
        return Reply("Done", "session")

    monkeypatch.setattr(HarborWorker, "run", worker)
    monkeypatch.setattr(
        "multi_round_harbor.agent.make_agent",
        lambda *_: SimpleNamespace(
            run=lambda _: pytest.fail("Reviewer must not be called after cancellation")
        ),
    )

    async def cancel():
        agent = MainAgent(
            logs_dir=tmp_path / "trial/agent",
            task_path=str(task),
            config_path=str(config),
            project=str(tmp_path),
        )
        context = SimpleNamespace(metadata=None)
        pending = asyncio.create_task(agent.run("", FilesystemEnvironment(), context))
        async with asyncio.timeout(2):
            while not started.is_set():
                await asyncio.sleep(0.001)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert finished.is_set()
        report = json.loads((tmp_path / "trial/rollout/report.json").read_text())
        assert report["status"] == "error"
        assert (
            report["stages"][0]["turns"][0]["error_details"]["phase"]
            == "progress_review"
        )
        assert context.metadata["run_id"] == report["run_id"]
        assert context.metadata["report_path"] == str(agent.run_dir / "report.json")

    asyncio.run(cancel())
