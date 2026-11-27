"""Verify run identities, evaluation scopes, and error attribution without relying on log text heuristics."""

import json
import sys

import pytest

from multi_round.enums import CallStatus, Phase, Role
from multi_round.models import ExternalExecutionError, Hook, Reply, UserMessage
from multi_round.agents.command import CommandAgent
from multi_round.runner import Runner
from multi_round.records import describe_error
from test_runner import run
from test_runner import setup as setup


def test_clarification_retry_and_trace_refs_have_stable_links(setup):
    task, user, worker, reviewer, out = setup
    task.stages = task.stages[:1]
    stage = task.stages[0]
    stage.max_turns, stage.retry_limit = 3, 1
    stage.default_reply = UserMessage(type="fixed", text="Include all orders")
    original_review = reviewer.fn

    def work(call):
        (call.workspace / "result.txt").write_text(
            "bad" if call.turn == 2 else "good", encoding="utf-8"
        )
        return Reply(
            "Please confirm scope" if call.turn == 1 else "Done",
            "worker-session",
            trace_refs=[f"test:{call.call_id}/generation-1"],
        )

    def review(call):
        if call.phase == "progress_review" and call.turn == 1:
            return Reply(
                json.dumps(
                    {
                        "reason": "Needs user confirmation",
                        "decision": "request_user_reply",
                        "continuation_id": None,
                    }
                )
            )
        return original_review(call)

    worker.fn, reviewer.fn = work, review
    report = run(setup)
    assert report["status"] == "completed" and report["retry_count"] == 1
    turns = report["stages"][0]["turns"]
    assert "evaluation_id" not in turns[0]
    assert turns[1]["outcome"] == "retry" and turns[2]["outcome"] == "passed"
    worker_ids = [call.call_id for call in worker.calls]
    first_scope, retry_scope = [t["evaluation_scope"] for t in turns[1:]]
    assert first_scope == {
        "worker_call_ids": worker_ids[:2],
        "attempt_worker_call_ids": worker_ids[:2],
        "message_range": [0, 4],
    }
    assert retry_scope == {
        "worker_call_ids": worker_ids,
        "attempt_worker_call_ids": worker_ids[2:],
        "message_range": [0, 6],
    }
    assert turns[1]["evaluation_id"] != turns[2]["evaluation_id"]
    assert turns[1]["evaluation"][0]["passed"] is False
    assert turns[2]["evaluation"][0]["passed"] is True
    assert [c.retry_count for c in worker.calls] == [0, 0, 1]
    call_ids = [c.call_id for c in user.calls + worker.calls + reviewer.calls]
    assert len(call_ids) == len(set(call_ids))
    assert len({t["turn_id"] for t in turns}) == 3
    for agent in (user, worker, reviewer):
        for call in agent.calls:
            assert isinstance(call.role, Role) and isinstance(call.phase, Phase)
            assert call.role == call.phase.agent_role
            assert call.run_id == report["run_id"] and call.stage_id == "s1"
            turn = turns[call.turn - 1]
            assert call.turn_id == turn["turn_id"]
            request = json.loads((call.log_dir / "request.json").read_text())
            assert all(request[key] == value for key, value in call.identity.items())
            assert request["prompt"] == call.prompt
            assert request["schema"] == call.schema
            assert request["log_dir"] == str(call.log_dir)
            assert not (call.log_dir / "prompt.txt").exists()
            record = next(c for c in turn["calls"] if c["call_id"] == call.call_id)
            assert record["status"] is CallStatus.RETURNED
            assert (out / record["log_dir"]).resolve() == call.log_dir.resolve()
            if call.role == "worker":
                assert record["trace_refs"] == [f"test:{call.call_id}/generation-1"]
    saved = json.loads((out / "report.json").read_text())
    assert saved == report
    # Enums are used internally while persisted JSON keeps plain strings for external adapters.
    for turn in saved["stages"][0]["turns"]:
        for call in turn["calls"]:
            assert type(call["role"]) is str and type(call["phase"]) is str
            assert call["status"] == "returned"
    rendered = (out / "report.md").read_text()
    assert report["run_id"] in rendered and worker_ids[2] in rendered
    # Re-running the same task allocates a fresh run_id that does not collide with prior rollouts.
    another = Runner(user, worker, reviewer, progress=lambda _: None).run(
        task, out.with_name("another")
    )
    assert another["run_id"] != report["run_id"]


@pytest.mark.parametrize(
    "error,category",
    [
        (ExternalExecutionError("model request timed out"), "external_execution"),
        (ValueError("model request timed out"), "unknown"),
        (KeyboardInterrupt(), "interrupted"),
    ],
)
def test_worker_exception_classification_uses_type_not_text(setup, error, category):
    def fail(call):
        raise error

    setup[2].fn = fail
    report = run(setup)
    turn = report["stages"][0]["turns"][0]
    details = turn["error_details"]
    assert report["status"] == "error" and "evaluation" not in turn
    assert details["category"] == category
    assert details["role"] == "worker" and details["phase"] == "worker"
    assert details["call_id"] == setup[2].calls[0].call_id
    assert details["exception_type"] == type(error).__name__
    call_error = turn["calls"][-1]["error_details"]
    # Propagating up the call stack adds frames, but error attribution and message stay identical.
    assert {k: v for k, v in call_error.items() if k != "traceback"} == {
        k: v for k, v in details.items() if k != "traceback"
    }
    assert "in fail" in call_error["traceback"]
    assert "in run_stages" in details["traceback"]
    assert json.loads((setup[2].calls[0].log_dir / "error.json").read_text()) == call_error


@pytest.mark.parametrize("suppress_cause", [False, True])
def test_error_traceback_preserves_chains_but_respects_suppression(suppress_cause):
    try:
        try:
            raise ValueError("underlying cause")
        except ValueError as cause:
            if suppress_cause:
                raise ExternalExecutionError("adapter failure") from None
            raise ExternalExecutionError("adapter failure") from cause
    except ExternalExecutionError as exc:
        details = describe_error(exc, phase=Phase.WORKER)

    assert "ExternalExecutionError: adapter failure" in details["traceback"]
    assert ("ValueError: underlying cause" in details["traceback"]) is not suppress_cause
    assert details["category"] == "external_execution"


@pytest.mark.parametrize("program_passed", [False, True])
def test_report_keeps_program_checks_when_reviewer_fails(setup, program_passed):
    task, user, worker, reviewer, out = setup
    original_review = reviewer.fn
    original_work = worker.fn

    def review(call):
        if call.phase == Phase.EVALUATION:
            raise ExternalExecutionError("Reviewer unavailable")
        return original_review(call)

    def work(call):
        reply = original_work(call)
        if not program_passed:
            (call.workspace / "result.txt").write_text("bad", encoding="utf-8")
        return reply

    worker.fn, reviewer.fn = work, review
    report = run(setup)
    turn = report["stages"][0]["turns"][0]
    assert report["status"] == "error" and "evaluation" not in turn
    assert turn["verification"]["checks"][0]["passed"] is program_passed
    rendered = (out / "report.md").read_text(encoding="utf-8")
    assert "Programmatic checks completed; full stage evaluation did not finish" in rendered
    assert "| artifact |" in rendered and "Read result.txt" in rendered
    assert f"| {'Pass' if program_passed else 'Fail'} |" in rendered
    assert "Traceback (most recent call last)" not in rendered


def test_complete_report_does_not_repeat_partial_checks(setup):
    report = run(setup)
    rendered = (setup[4] / "report.md").read_text(encoding="utf-8")
    assert report["status"] == "completed"
    assert "full stage evaluation did not finish" not in rendered
    assert rendered.count("| artifact |") == len(report["stages"])


def test_empty_worker_output_is_protocol_error_not_task_failure(setup):
    setup[2].fn = lambda call: Reply("")
    report = run(setup)
    turn = report["stages"][0]["turns"][0]
    assert turn["error_details"]["category"] == "output_protocol"
    assert turn["error_details"]["role"] == "worker"
    assert "evaluation" not in turn and report["retry_count"] == 0


def test_error_log_failure_preserves_original_worker_exception(setup, monkeypatch):
    from multi_round import records

    write_json = records.write_json

    def save(path, value):
        if path.name == "error.json":
            raise OSError("log directory unwritable")
        write_json(path, value)

    def fail(call):
        raise ExternalExecutionError("model endpoint connection failed")

    monkeypatch.setattr(records, "write_json", save)
    setup[2].fn = fail
    report = run(setup)
    turn = report["stages"][0]["turns"][0]
    assert turn["error_details"]["message"] == "model endpoint connection failed"
    assert turn["error_details"]["exception_type"] == "ExternalExecutionError"
    assert turn["error_notes"] == ["Failed to save call error log: log directory unwritable"]
    saved = json.loads((setup[4] / "report.json").read_text(encoding="utf-8"))
    assert saved == report


@pytest.mark.parametrize("phase", ["user_message", "progress_review", "evaluation"])
def test_invalid_judge_or_user_output_is_not_worker_error(setup, phase):
    agent = setup[1] if phase == "user_message" else setup[3]
    original = agent.fn
    invalid = "" if phase == "user_message" else "invalid-json"

    def respond(call):
        if call.phase != phase:
            return original(call)
        reply = Reply("temporary text")
        reply.text = invalid
        return reply

    agent.fn = respond
    report = run(setup)
    turn = report["stages"][0]["turns"][0]
    details = turn["error_details"]
    assert details["category"] == "output_protocol" and details["phase"] == phase
    assert details["role"] == ("user" if phase == "user_message" else "reviewer")
    assert details["call_id"] == agent.calls[-1].call_id
    assert (
        json.loads((agent.calls[-1].log_dir / "reply.json").read_text())["text"]
        == invalid
    )
    assert "evaluation" not in turn


def test_verifier_failures_have_operation_but_no_agent_role(setup):
    task, user, worker, reviewer, out = setup
    task.verifier = [sys.executable, "-c", "raise SystemExit(2)"]
    report = run(setup)
    details = report["stages"][0]["turns"][0]["error_details"]
    assert details["role"] is None and details["phase"] == "verification"
    assert details["category"] == "external_execution" and details["call_id"] is None
    task.verifier = [sys.executable, "-c", "print('invalid-json')"]
    report = Runner(user, worker, reviewer, progress=lambda _: None).run(
        task, out.with_name("bad-protocol")
    )
    details = report["stages"][0]["turns"][0]["error_details"]
    assert details["category"] == "output_protocol"
    assert details["role"] is None and details["phase"] == "verification"
    assert details["call_id"] is None


@pytest.mark.parametrize("phase", ["prepare_submission", "after_worker"])
def test_workspace_and_hook_failures_do_not_inherit_worker_role(
    setup, monkeypatch, phase
):
    task, user, worker, reviewer, out = setup

    def fail(*args):
        raise OSError("file operation failed")

    handlers = {}
    if phase == "prepare_submission":
        monkeypatch.setattr("multi_round.workspaces.snapshot", fail)
    else:
        task.hooks = [
            Hook(
                "fail",
                "after_worker",
                description="Simulate file failure",
                stage_id="s1",
                handler="fail",
            )
        ]
        handlers["fail"] = fail
    report = Runner(
        user, worker, reviewer, hook_handlers=handlers, progress=lambda _: None
    ).run(task, out)
    turn = report["stages"][0]["turns"][0]
    details = turn["error_details"]
    assert details["phase"] == phase and details["category"] == "external_execution"
    assert details["role"] is None and details["call_id"] is None
    assert any(
        c["role"] == "worker" and c["status"] == "returned" for c in turn["calls"]
    )
    assert "evaluation" not in turn and report["status"] == "error"
    saved = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert saved["stages"][0]["turns"][0]["error_details"]["role"] is None
    rendered = (out / "report.md").read_text(encoding="utf-8")
    assert f"Error phase: {phase}" in rendered and "agent role: None" not in rendered
    if phase == "after_worker":
        assert report["hooks"][0]["error_details"]["role"] is None
        assert report["hooks"][0]["error_details"]["phase"] == phase


def test_cleanup_failure_keeps_worker_error_and_its_identity(setup):
    task, user, worker, reviewer, out = setup
    task.hooks = [
        Hook("cleanup", "after_run", description="Release environment", handler="cleanup")
    ]
    worker.fn = lambda call: Reply("")

    def cleanup(event):
        raise OSError("environment release failed")

    report = Runner(
        user,
        worker,
        reviewer,
        hook_handlers={"cleanup": cleanup},
        progress=lambda _: None,
    ).run(task, out)
    assert report["stages"][0]["turns"][0]["error_details"]["role"] == "worker"
    assert report["hooks"][0]["error_details"]["role"] is None
    assert report["hooks"][0]["error_details"]["phase"] == "after_run"
    assert report["hooks"][0]["error_details"]["category"] == "external_execution"
    assert report["cleanup_error_details"]["role"] is None
    assert report["cleanup_error_details"]["phase"] == "after_run"
    assert json.loads((out / "report.json").read_text()) == report


def test_command_wrapper_receives_identity_and_returns_trace_refs(setup):
    task, user, _, reviewer, out = setup
    task.stages = task.stages[:1]
    task.stages[0].verifier_checks = []
    task.stages[0].needs_workspace = False
    reviewer.fn = lambda call: Reply(
        json.dumps(
            {
                "reason": "Submitted",
                "decision": "review_submission",
                "continuation_id": None,
            }
            if call.phase == "progress_review"
            else {
                "reason": "Passed",
                "criteria": [{"id": "explanation", "passed": True, "evidence": "Replied"}],
            }
        )
    )
    command = CommandAgent(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; p=json.load(sys.stdin); "
                "assert p['stage_id']=='s1' and p['turn']==1 and p['retry_count']==0; "
                "assert p['run_id'] and p['turn_id']; "
                "print(json.dumps({'text':'Done','trace_refs':['gateway:'+p['call_id']]}))"
            ),
        ]
    )
    report = Runner(user, command, reviewer, progress=lambda _: None).run(task, out)
    assert report["status"] == "completed"
    worker_call = next(
        c for c in report["stages"][0]["turns"][0]["calls"] if c["role"] == "worker"
    )
    assert worker_call["trace_refs"] == ["gateway:" + worker_call["call_id"]]


def test_stage_scope_does_not_mix_previous_stage_worker_calls(setup):
    report = run(setup)
    prior_ids = set()
    for stage in report["stages"]:
        turn = stage["turns"][0]
        ids = set(turn["evaluation_scope"]["worker_call_ids"])
        assert len(ids) == 1 and not ids & prior_ids
        assert turn["evaluation_scope"]["message_range"] == [
            stage["start_message"],
            stage["start_message"] + 2,
        ]
        prior_ids.update(ids)
