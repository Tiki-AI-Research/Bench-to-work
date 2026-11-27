"""Harbor entrypoint configuration and cleanup recording using real Runner and deterministic doubles."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from harbor.models.trial.paths import TrialPaths
from multi_round_harbor.agent import MainAgent
from multi_round_harbor.verifier import ReportVerifier
from multi_round_harbor.worker import HarborWorker

from multi_round.enums import Phase
from multi_round.models import ExternalExecutionError, Reply
from multi_round.runner import Runner


@pytest.mark.parametrize("worker_fails", [False, True])
def test_cleanup_error_reaches_report_and_harbor_without_erasing_evaluation(
    tmp_path, monkeypatch, worker_fails
):
    task = {
        "id": "cleanup",
        "title": "Cleanup",
        "persona": "User",
        "background": "Test",
        "stages": [
            {
                "id": "s1",
                "title": "Execute",
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
    task_path, config_path = tmp_path / "task.json", tmp_path / "config.json"
    task_path.write_text(json.dumps(task), encoding="utf-8")
    config_path.write_text(
        json.dumps(
            {
                "user": {"name": "user"},
                "reviewer": {"name": "reviewer"},
                "worker": {"command": ["unused"]},
                "verifier_timeout": 7,
            }
        ),
        encoding="utf-8",
    )
    closed = []

    class InternalAgent:
        def __init__(self, name):
            self.name = name

        def run(self, call):
            if call.phase == Phase.PROGRESS_REVIEW:
                return Reply(
                    json.dumps(
                        {
                            "reason": "Submitted",
                            "decision": "review_submission",
                            "continuation_id": None,
                        }
                    )
                )
            return Reply(
                json.dumps(
                    {
                        "reason": "Completed",
                        "criteria": [
                            {"id": "done", "evidence": "Finished", "passed": True}
                        ],
                    }
                )
            )

        def close(self):
            closed.append(self.name)
            raise RuntimeError(f"{self.name} close failed")

    def worker(self, call):
        if worker_fails:
            raise ExternalExecutionError("worker offline")
        return Reply("Finished", "session")

    def configured_runner(**kwargs):
        assert kwargs["verifier_timeout"] == 7
        return Runner(**kwargs)

    monkeypatch.setattr(
        "multi_round_harbor.agent.make_agent", lambda c, _: InternalAgent(c["name"])
    )
    monkeypatch.setattr("multi_round_harbor.agent.Runner", configured_runner)
    monkeypatch.setattr(HarborWorker, "run", worker)
    agent = MainAgent(
        logs_dir=tmp_path / "trial/agent",
        task_path=str(task_path),
        config_path=str(config_path),
        project=str(tmp_path),
    )
    context = SimpleNamespace(metadata=None)
    with pytest.raises(
        ExternalExecutionError if worker_fails else RuntimeError,
        match="worker offline" if worker_fails else "reviewer close failed",
    ):
        asyncio.run(agent.run("", SimpleNamespace(), context))

    assert closed == ["reviewer", "user"]
    report = json.loads((agent.run_dir / "report.json").read_text(encoding="utf-8"))
    assert report["status"] == context.metadata["status"] == "error"
    assert len(report["agent_cleanup_errors"]) == 2
    assert all(e["phase"] == "agent_cleanup" for e in report["agent_cleanup_errors"])
    turn = report["stages"][0]["turns"][0]
    if worker_fails:
        assert turn["error_details"]["phase"] == "worker"
        assert "worker offline" in report["reason"]
    else:
        assert turn["evaluation"][0]["passed"]
    verifier = ReportVerifier(
        task=None,
        trial_paths=TrialPaths(tmp_path / "trial"),
        environment=None,
        reward_function="multi_round_harbor.verifier:final_check_fraction",
    )
    with pytest.raises(ValueError, match="scorable"):
        asyncio.run(verifier.verify())
