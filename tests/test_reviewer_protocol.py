"""Reviewer protocol retries do not re-run the Worker, verifier, or hooks, nor hide failed outputs."""

import json

import pytest
from test_pi import model_server as model_server
from test_pi import pi_agent as pi_agent
from test_runner import run
from test_runner import setup as setup

from multi_round.models import (
    ExternalExecutionError,
    Hook,
    IncompleteResponseError,
    Reply,
)
from multi_round.runner import Runner


def valid_reply(phase):
    if phase == "progress_review":
        return (
            '{"reason":"Delivered","decision":"review_submission","continuation_id":null}'
        )
    return '{"reason":"Verified","criteria":[{"id":"explanation","passed":true,"evidence":"Reply explained the update"}]}'


@pytest.mark.parametrize("phase", ["progress_review", "evaluation"])
@pytest.mark.parametrize("problem", ["json", "schema", "ids"])
def test_protocol_correction_keeps_one_turn_and_all_original_records(
    setup, phase, problem
):
    task, user, worker, reviewer, out = setup
    task.stages = task.stages[:1]
    original = reviewer.fn
    bad = "not valid JSON"
    if problem == "schema":
        bad = '{"unexpected":true}'
    elif problem == "ids":
        bad = (
            '{"reason":"Needs reply","decision":"request_user_reply","continuation_id":"unknown"}'
            if phase == "progress_review"
            else '{"reason":"Checked","criteria":[]}'
        )
    attempts = []

    def respond(call):
        if call.phase != phase:
            return original(call)
        attempts.append(call)
        if call.protocol_retry_count == 0:
            return Reply(bad, trace_refs=["fixture:initial"])
        assert call.protocol_retry_count == 1
        assert call.protocol_retry_of == attempts[0].call_id
        assert call.session_id is None
        assert call.workspace == attempts[0].workspace
        assert (
            call.schema == attempts[0].schema
            and call.attachments == attempts[0].attachments
        )
        assert (
            attempts[0].prompt in call.prompt
            and json.dumps(bad, ensure_ascii=False) in call.prompt
        )
        return Reply(valid_reply(phase), trace_refs=["fixture:correction"])

    reviewer.fn = respond
    events = []
    task.hooks = [
        Hook(
            "before",
            "before_user",
            description="Record pre-user event",
            handler="mark",
            stage_id="s1",
        ),
        Hook(
            "after",
            "after_worker",
            description="Record post-worker event",
            handler="mark",
            stage_id="s1",
        ),
    ]
    runner = Runner(
        user,
        worker,
        reviewer,
        progress=lambda _: None,
        hook_handlers={"mark": lambda event: events.append(event["event"])},
    )
    verification_calls = []
    original_verify = runner.verify

    def verify(*args):
        verification_calls.append(args)
        return original_verify(*args)

    runner.verify = verify
    report = runner.run(task, out)
    assert report["status"] == "completed"
    assert report["retry_count"] == 0 and len(report["stages"][0]["turns"]) == 1
    assert len(user.calls) == len(worker.calls) == len(verification_calls) == 1
    assert events == ["before_user", "after_worker"]
    assert len(attempts) == 2 and attempts[0].turn_id == attempts[1].turn_id

    turn = report["stages"][0]["turns"][0]
    records = [c for c in turn["calls"] if c["phase"] == phase]
    assert "error_details" not in turn
    assert records[0]["status"] == records[1]["status"] == "returned"
    assert records[0]["protocol_error"]["category"] == "output_protocol"
    assert "protocol_error" not in records[1]
    assert records[1]["protocol_retry_of"] == records[0]["call_id"]
    assert records[0]["trace_refs"] == ["fixture:initial"]
    assert records[1]["trace_refs"] == ["fixture:correction"]
    assert (
        json.loads((out / records[0]["log_dir"] / "reply.json").read_text())["text"]
        == bad
    )
    assert (out / records[0]["log_dir"] / "protocol_error.json").exists()
    assert (out / records[1]["log_dir"] / "request.json").exists()
    text = (out / "report.md").read_text()
    assert "Protocol retry #1" in text and records[0]["call_id"] in text
    assert json.loads((out / "report.json").read_text()) == report


@pytest.mark.parametrize("limit", [0, 1, 2])
@pytest.mark.parametrize("phase", ["progress_review", "evaluation"])
def test_protocol_budget_exhaustion_is_internal_error_not_worker_failure(
    setup, phase, limit
):
    task, _, worker, reviewer, out = setup
    task.stages[0].retry_limit = 2
    original = reviewer.fn
    reviewer.fn = lambda c: Reply("invalid") if c.phase == phase else original(c)
    report = run(setup, reviewer_protocol_retry_limit=limit)
    assert report["status"] == "error" and report["retry_count"] == 0
    assert len(worker.calls) == 1
    assert report["stages"][1]["status"] == "pending"
    turn = report["stages"][0]["turns"][0]
    records = [c for c in turn["calls"] if c["phase"] == phase]
    assert len(records) == limit + 1
    assert [c["protocol_retry_count"] for c in records] == list(range(limit + 1))
    assert all(c["protocol_error"]["category"] == "output_protocol" for c in records)
    assert turn["error_details"]["call_id"] == records[-1]["call_id"]
    assert "evaluation" not in turn and "failed_hard_gates" not in turn
    for record in records:
        assert (
            json.loads((out / record["log_dir"] / "reply.json").read_text())["text"]
            == "invalid"
        )


@pytest.mark.parametrize(
    "error", [ExternalExecutionError("service unavailable"), TimeoutError("timed out")]
)
def test_call_failure_does_not_trigger_protocol_correction(setup, error):
    reviewer = setup[3]

    def fail(call):
        raise error

    reviewer.fn = fail
    report = run(setup, reviewer_protocol_retry_limit=2)
    assert report["status"] == "error" and len(reviewer.calls) == 1
    turn = report["stages"][0]["turns"][0]
    assert turn["error_details"]["category"] == "external_execution"
    assert "protocol_error" not in turn["calls"][-1]


def test_retry_call_failure_preserves_previous_protocol_error(setup):
    reviewer = setup[3]

    def respond(call):
        if call.protocol_retry_count:
            raise ExternalExecutionError("retry connection dropped")
        return Reply("invalid")

    reviewer.fn = respond
    report = run(setup, reviewer_protocol_retry_limit=2)
    turn = report["stages"][0]["turns"][0]
    records = [c for c in turn["calls"] if c["role"] == "reviewer"]
    assert len(records) == 2 and report["status"] == "error"
    assert records[0]["protocol_error"]["category"] == "output_protocol"
    assert records[1]["protocol_retry_of"] == records[0]["call_id"]
    assert records[1]["error_details"]["category"] == "external_execution"


def test_corrected_false_remains_false_and_triggers_business_gate(setup):
    task, _, worker, reviewer, _ = setup
    task.stages = task.stages[:1]
    stage = task.stages[0]
    stage.criteria[0].hard_gate = True
    stage.retry_limit = 1
    original = reviewer.fn

    def respond(call):
        if call.phase != "evaluation":
            return original(call)
        data = json.loads(valid_reply(call.phase))
        if call.turn == 1:
            data["criteria"][0]["passed"] = False
        if call.turn == 1 and call.protocol_retry_count == 0:
            data["extra"] = True
        return Reply(json.dumps(data))

    reviewer.fn = respond
    report = run(setup, reviewer_protocol_retry_limit=1)
    assert report["status"] == "completed" and report["retry_count"] == 1
    turns = report["stages"][0]["turns"]
    assert turns[0]["outcome"] == "retry"
    assert turns[0]["evaluation"][1]["passed"] is False
    assert turns[0]["failed_hard_gates"] == ["explanation"]
    assert [c.retry_count for c in worker.calls[:2]] == [0, 1]
    assert (
        len([c for c in reviewer.calls if c.phase == "evaluation" and c.turn == 1]) == 2
    )


def test_valid_failure_and_error_decision_are_not_protocol_retried(setup):
    reviewer = setup[3]
    reviewer.fn = lambda c: Reply(
        '{"reason":"Cannot determine","decision":"error","continuation_id":null}'
    )
    report = run(setup, reviewer_protocol_retry_limit=2)
    assert report["status"] == "error" and len(reviewer.calls) == 1
    assert "protocol_error" not in report["stages"][0]["turns"][0]["calls"][-1]


def test_multiple_corrections_reference_first_call_and_last_reply(setup):
    task, _, _, reviewer, _ = setup
    task.stages = task.stages[:1]
    original = reviewer.fn
    attempts = []

    def respond(call):
        if call.phase != "evaluation":
            return original(call)
        attempts.append(call)
        if call.protocol_retry_count == 0:
            return Reply('{"reason":"Missing items","criteria":[]}')
        if call.protocol_retry_count == 1:
            return Reply('{"reason":"Still missing items on second try","criteria":[]}')
        assert "Still missing items on second try" in call.prompt
        assert call.protocol_retry_of == attempts[0].call_id
        return Reply(valid_reply(call.phase))

    reviewer.fn = respond
    report = run(setup, reviewer_protocol_retry_limit=2)
    assert report["status"] == "completed" and report["retry_count"] == 0
    assert len(attempts) == 3
    assert attempts[0].prompt in attempts[-1].prompt
    assert [c.protocol_retry_count for c in attempts] == [0, 1, 2]


@pytest.mark.parametrize("limit", [-1, True, 1.5, "1"])
def test_invalid_protocol_budget_is_rejected(setup, limit):
    _, user, worker, reviewer, _ = setup
    with pytest.raises(ValueError, match="non-negative integer"):
        Runner(user, worker, reviewer, reviewer_protocol_retry_limit=limit)


def test_cli_passes_protocol_budget_without_passing_it_to_agents(
    setup, monkeypatch, tmp_path
):
    from multi_round import cli

    task, user, worker, reviewer, out = setup
    task.stages = task.stages[:1]
    config = {
        "user": {},
        "worker": {},
        "reviewer": {},
        "reviewer_protocol_retry_limit": 0,
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    created = iter([user, worker, reviewer])
    monkeypatch.setattr(cli, "load_task", lambda _: task)
    monkeypatch.setattr(cli, "make_agent", lambda options, project: next(created))
    reviewer.fn = lambda c: Reply("invalid")
    assert cli.main(["--task", str(tmp_path / "task.json"), "--config", str(config_path), "--output", str(out)]) == 1
    assert len(reviewer.calls) == 1
    assert (
        json.loads((out / "config.json").read_text())["reviewer_protocol_retry_limit"]
        == 0
    )


def test_valid_false_does_not_trigger_protocol_retry(setup):
    task, _, _, reviewer, _ = setup
    task.stages = task.stages[:1]
    original = reviewer.fn

    def respond(call):
        if call.phase != "evaluation":
            return original(call)
        data = json.loads(valid_reply(call.phase))
        data["criteria"][0]["passed"] = False
        return Reply(json.dumps(data))

    reviewer.fn = respond
    report = run(setup, reviewer_protocol_retry_limit=2)
    assert report["status"] == "completed" and report["retry_count"] == 0
    turn = report["stages"][0]["turns"][0]
    assert turn["outcome"] == "advanced_with_errors"
    assert len([c for c in reviewer.calls if c.phase == "evaluation"]) == 1
    assert not any("protocol_error" in c for c in turn["calls"])


@pytest.mark.parametrize("structured_output", [True, False])
def test_pi_protocol_correction_uses_same_evidence_with_fresh_session(
    setup, pi_agent, model_server, structured_output
):
    """Real Pi SDK performs read-only inspection against the same frozen snapshot across fresh retry sessions."""
    task, user, worker, progress_reviewer, out = setup
    task.stages = task.stages[:1]
    pi_agent.config["structured_output"] = structured_output

    def respond(payload):
        last = payload["messages"][-1]
        if last["role"] != "tool":
            return {
                "name": "read",
                "arguments": '{"path":"submission/result.txt"}',
            }
        assert last["content"] == "good"
        correcting = "previous evaluation output failed" in json.dumps(
            payload["messages"], ensure_ascii=False
        )
        return (
            valid_reply("evaluation")
            if correcting
            else '{"reason":"Read submitted artifact","criteria":[]}'
        )

    model_server[2][0] = respond
    report = Runner(
        user,
        worker,
        pi_agent,
        progress_reviewer=progress_reviewer,
        progress=lambda _: None,
    ).run(task, out)
    assert report["status"] == "completed" and report["retry_count"] == 0
    assert len(worker.calls) == 1 and len(model_server[1]) == 4
    turn = report["stages"][0]["turns"][0]
    records = [c for c in turn["calls"] if c["phase"] == "evaluation"]
    assert len(records) == 2 and "protocol_error" in records[0]
    assert records[1]["protocol_retry_count"] == 1
    assert records[1]["protocol_retry_of"] == records[0]["call_id"]
    requests = [
        json.loads((out / c["log_dir"] / "request.json").read_text()) for c in records
    ]
    assert requests[0]["workspace"] == requests[1]["workspace"]
    for c in records:
        assert "tool_execution_end" in (out / c["log_dir"] / "stderr.log").read_text()


@pytest.mark.parametrize("phase", ["progress_review", "evaluation"])
def test_incomplete_response_retries_without_protocol_correction_or_worker_rerun(
    setup, phase
):
    task, user, worker, reviewer, out = setup
    task.stages = task.stages[:1]
    original = reviewer.fn
    attempts = []

    def respond(call):
        if call.phase != phase:
            return original(call)
        attempts.append(call)
        if len(attempts) == 1:
            raise IncompleteResponseError("model output truncated at length limit")
        assert call.protocol_retry_count == 0
        assert call.protocol_retry_of is None
        assert call.prompt == attempts[0].prompt
        assert call.workspace == attempts[0].workspace
        assert call.schema == attempts[0].schema
        return Reply(valid_reply(phase), trace_refs=["fixture:retried"])

    reviewer.fn = respond
    report = Runner(
        user,
        worker,
        reviewer,
        progress=lambda _: None,
        reviewer_protocol_retry_limit=1,
    ).run(task, out)
    assert report["status"] == "completed" and report["retry_count"] == 0
    assert len(user.calls) == len(worker.calls) == 1
    assert len(attempts) == 2

    turn = report["stages"][0]["turns"][0]
    records = [c for c in turn["calls"] if c["phase"] == phase]
    assert [c["status"] for c in records] == ["error", "returned"]
    assert records[0]["error_details"]["category"] == "external_execution"
    assert records[0]["error_details"]["exception_type"] == "IncompleteResponseError"
    assert (out / records[0]["log_dir"] / "error.json").exists()
    assert not (out / records[0]["log_dir"] / "protocol_error.json").exists()
    assert records[1]["log_dir"].endswith("/incomplete_retry_1")
    assert records[1]["protocol_retry_count"] == 0
    assert records[1]["protocol_retry_of"] is None
    assert "Protocol retry" not in (out / "report.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("limit", [0, 1])
def test_incomplete_response_respects_retry_budget_and_reports_last_error(setup, limit):
    task, _, worker, reviewer, _ = setup
    task.stages = task.stages[:1]
    original = reviewer.fn

    def respond(call):
        if call.phase == "evaluation":
            raise IncompleteResponseError("model output truncated at length limit")
        return original(call)

    reviewer.fn = respond
    report = run(setup, reviewer_protocol_retry_limit=limit)
    assert report["status"] == "error" and report["retry_count"] == 0
    assert len(worker.calls) == 1
    turn = report["stages"][0]["turns"][0]
    records = [c for c in turn["calls"] if c["phase"] == "evaluation"]
    assert len(records) == limit + 1
    assert all(c["status"] == "error" for c in records)
    assert turn["error_details"]["category"] == "external_execution"
    assert turn["error_details"]["call_id"] == records[-1]["call_id"]


def test_openai_model_length_triggers_runner_retry(setup, monkeypatch):
    import httpx
    from openai import AsyncOpenAI
    from multi_round.agents import openai_model
    from test_openai_model import completion

    task, user, worker, reviewer, out = setup
    task.stages = task.stages[:1]
    monkeypatch.setenv("TEST_MODEL_KEY", "local-test-key")
    responses = iter(
        [
            completion("truncated prefix", "length"),
            completion(valid_reply("progress_review"), "stop"),
        ]
    )

    def client(**options):
        transport = httpx.MockTransport(
            lambda _req: httpx.Response(200, json=next(responses))
        )
        return AsyncOpenAI(**options, http_client=httpx.AsyncClient(transport=transport))

    monkeypatch.setattr(openai_model, "AsyncOpenAI", client)
    progress = openai_model.OpenAICompatibleModel(
        "https://example.invalid/v1",
        "test",
        key_env="TEST_MODEL_KEY",
        stream=False,
    )
    report = Runner(
        user,
        worker,
        reviewer,
        progress_reviewer=progress,
        progress=lambda _: None,
        reviewer_protocol_retry_limit=1,
    ).run(task, out)
    assert report["status"] == "completed", report["reason"]
    records = [c for c in report["stages"][0]["turns"][0]["calls"] if c["phase"] == "progress_review"]
    assert [c["status"] for c in records] == ["error", "returned"]
    assert records[0]["error_details"]["exception_type"] == "IncompleteResponseError"


@pytest.mark.parametrize("identical", [True, False])
def test_duplicate_json_objects_rejected_and_recovered_via_protocol_retry(setup, identical):
    """Duplicate JSON objects (both verbatim and self-edited) are rejected without silent guessing and recovered via protocol retry."""
    from multi_round.json_protocol import parse_model_json
    from multi_round.models import OutputProtocolError

    task, _, _, reviewer, out = setup
    task.stages = task.stages[:1]
    original = reviewer.fn
    first_obj = valid_reply("evaluation")
    second_obj = first_obj if identical else json.dumps(
        {
            **json.loads(first_obj),
            "reason": "Updated path reference without changing booleans",
        }
    )
    double_raw = f"{first_obj}{second_obj}" if identical else f"{first_obj}\n{second_obj}"

    with pytest.raises(OutputProtocolError, match="Multiple valid JSON"):
        parse_model_json(double_raw)

    def respond(call):
        if call.phase != "evaluation":
            return original(call)
        return Reply(second_obj if call.protocol_retry_count else double_raw)

    reviewer.fn = respond
    report = run(setup, reviewer_protocol_retry_limit=1)
    assert report["status"] == "completed" and report["retry_count"] == 0
    records = [c for c in report["stages"][0]["turns"][0]["calls"] if c["phase"] == "evaluation"]
    assert len(records) == 2
    assert records[0]["protocol_error"]["category"] == "output_protocol"
    assert records[1]["protocol_retry_count"] == 1
    assert json.loads((out / records[0]["log_dir"] / "reply.json").read_text())["text"] == double_raw
