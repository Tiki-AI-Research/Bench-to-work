"""Recoverable JSON syntax never implies inferred verdicts; verify Reviewer vs. User/Worker boundaries."""

import json
from importlib.metadata import version

import pytest
from test_runner import run
from test_runner import setup as setup

from multi_round.json_protocol import (
    parse_and_validate_json,
    parse_model_json,
    validate_json,
)
from multi_round.models import (
    PROGRESS_SCHEMA,
    REVIEW_SCHEMA,
    TEXT,
    OutputProtocolError,
    Reply,
    object_schema,
)

SAMPLE_SCHEMA = object_schema({"message": TEXT})


@pytest.mark.parametrize(
    "wrapper",
    [
        lambda value: f"Checked submission; result follows:\n{value}",
        lambda value: f"```json\n{value}\n```",
        lambda value: f"Evaluation result:\n```json\n{value}\n```\nEnd of review.",
    ],
)
def test_extract_unique_object_preserves_strings_and_false(wrapper):
    expected = {
        "reason": 'Original text includes {"passed": true}, [note], and apostrophe don\'t',
        "criteria": [{"id": "answer", "passed": False, "evidence": "Value mismatch"}],
    }
    raw = wrapper(json.dumps(expected, ensure_ascii=False))
    actual, recovery = parse_model_json(raw)
    assert validate_json(actual, REVIEW_SCHEMA) == expected
    assert recovery["method"] == "extracted"
    start, end = recovery["source_range"]
    assert json.loads(raw[start:end]) == expected


@pytest.mark.parametrize(
    "wrapper",
    [
        lambda value: f"D2={{forms:42, spend:900}}, check correction.\n{value}",
        lambda value: (
            f"Compare {{old, new}} across [forms, spend].\n```json\n{value}\n```"
        ),
        lambda value: f"D2={{forms:42}}, D3={{forms:35}}.\n{value}\nSee [notes].",
    ],
)
def test_prose_containers_do_not_compete_with_valid_json(wrapper, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Must not invoke json-repair when a unique valid JSON object exists")

    monkeypatch.setattr("multi_round.json_protocol.repair_json", unexpected)
    expected = {
        "reason": "Checked against correction log",
        "criteria": [{"id": "correction", "passed": False, "evidence": "Not updated yet"}],
    }
    raw = wrapper(json.dumps(expected, ensure_ascii=False))
    data, recovery = parse_model_json(raw)
    assert validate_json(data, REVIEW_SCHEMA) == expected
    assert recovery["method"] == "extracted"
    left, right = recovery["source_range"]
    assert json.loads(raw[left:right]) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "{'reason': 'Delivered', 'decision': 'review_submission', 'continuation_id': null}",
        '{"reason": "Delivered" "decision": "review_submission", "continuation_id": null}',
        '{"reason": "Delivered", "decision": "review_submission", "continuation_id": null,}',
        '{"reason": "Delivered", "decision": "review_submission", "continuation_id": null',
        '{reason: "Delivered", decision: "review_submission", continuation_id: null}',
    ],
)
def test_common_syntax_errors_recover_explicit_values(raw):
    data, recovery = parse_model_json(raw)
    assert validate_json(data, PROGRESS_SCHEMA) == {
        "reason": "Delivered",
        "decision": "review_submission",
        "continuation_id": None,
    }
    assert recovery["method"] == "repaired"
    assert recovery["parser"] == f"json-repair/{version('json-repair')}"
    assert recovery["value"] == data


@pytest.mark.parametrize(
    "raw",
    [
        'Example {"message": "sample"}\nActual {"message": "real"}',
        'Bad example {"unexpected": true}\nActual {"message": "real"}',
        '```json\n{"message": "sample"}\n```\n```json\n{"message": "real"}\n```',
        '{"message": "complete"}\n{"message":',
        '{"message": "first", "message": "second"}',
        '{"message": "unfinished',
        '{"message":}',
        '{"message"}',
        "{'message': 'first'}\n{'message': 'second'}",
        'Draft {"message": "first", "message": "second"}\nFinal {"message": "final"}',
        "No JSON provided",
    ],
)
def test_ambiguous_or_missing_content_is_not_recovered(raw):
    with pytest.raises(OutputProtocolError):
        parse_model_json(raw)


@pytest.mark.parametrize(
    "raw, schema",
    [
        ('{"message": "real message", "extra": "unexpected"}', SAMPLE_SCHEMA),
        (
            '{"reason": "done", "decision": "submit", "continuation_id": null}',
            PROGRESS_SCHEMA,
        ),
        ('{"reason": "done", "decision": "review_submission"}', PROGRESS_SCHEMA),
        (
            '{"reason": "done", "decision": "review_submission", "continuation_id":}',
            PROGRESS_SCHEMA,
        ),
        ('[{"message": "do not unwrap arrays"}]', SAMPLE_SCHEMA),
        (
            '{"reason": "review", "criteria": [{"id": "answer", "passed": null, "evidence": "bad"}]}',
            REVIEW_SCHEMA,
        ),
        (
            '{"reason": "fail", "criteria": [{"id": "answer", "passed": "false", "evidence": "bad"}]}',
            REVIEW_SCHEMA,
        ),
        (
            "{'reason': 'fail', 'criteria': [{'id': 'answer', 'passed': 'false', 'evidence': 'bad'}]}",
            REVIEW_SCHEMA,
        ),
    ],
)
def test_recovery_does_not_fill_schema_or_convert_decisions(raw, schema):
    with pytest.raises(OutputProtocolError):
        data, _ = parse_model_json(raw)
        validate_json(data, schema)


def test_valid_json_is_never_sent_to_repair(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Valid JSON must bypass json-repair")

    monkeypatch.setattr("multi_round.json_protocol.repair_json", unexpected)
    expected = {"message": "Literal \\, newline\nand Unicode: café"}
    data, recovery = parse_model_json(json.dumps(expected))
    assert data == expected and recovery is None


def test_trailing_dangling_quote_is_repaired_without_changing_message():
    # Extra quote after a closed string; User messages never pass through this JSON parser.
    raw = '{"message":"Ledger sent; please verify.","}'
    data, recovery = parse_model_json(raw)
    assert validate_json(data, SAMPLE_SCHEMA) == {"message": "Ledger sent; please verify."}
    assert recovery["method"] == "repaired"


def test_repair_library_result_is_recorded_without_claiming_content_equivalence():
    # json-repair merges duplicate keys in non-standard single-quoted objects; keep this boundary explicit.
    data, recovery = parse_model_json("{'message': 'first', 'message': 'later'}")
    assert validate_json(data, SAMPLE_SCHEMA) == {"message": "later"}
    assert recovery["method"] == "repaired" and recovery["value"] == data


def test_reviewers_recover_while_user_and_worker_keep_plain_text(setup):
    task, user, worker, reviewer, out = setup
    task.stages = task.stages[:1]
    originals = {id(reviewer): reviewer.fn}
    raw_replies = {}

    def wrap(agent):
        def respond(call):
            original = originals[id(agent)](call)
            text = f"Checked D2={{forms:42, spend:900}}; verdict:\n```json\n{original.text}\n```"
            raw_replies[call.call_id] = text
            return Reply(text)

        return respond

    reviewer.fn = wrap(reviewer)
    original_work = worker.fn
    worker_text = 'Completed; summary:\n```json\n{"amount": 30}\n```'

    def work(call):
        reply = original_work(call)
        return Reply(worker_text, reply.session_id)

    worker.fn = work
    report = run(setup)
    assert report["status"] == "completed" and report["retry_count"] == 0
    turn = report["stages"][0]["turns"][0]
    assert turn["user"] == "Complete round 1"
    assert worker.calls[0].prompt == turn["user"]
    assert turn["assistant"] == worker_text
    assert len(user.calls) == len(worker.calls) == 1 and len(reviewer.calls) == 2
    for record in turn["calls"]:
        log_dir = out / record["log_dir"]
        raw = json.loads((log_dir / "reply.json").read_text(encoding="utf-8"))["text"]
        if record["role"] in ("user", "worker"):
            assert raw == (turn["user"] if record["role"] == "user" else worker_text)
            assert "json_recovery" not in record
            assert not (log_dir / "json_recovery.json").exists()
            continue
        assert raw == raw_replies[record["call_id"]]
        audit = json.loads((out / record["json_recovery"]).read_text(encoding="utf-8"))
        assert audit["call_id"] == record["call_id"] and audit["method"] == "extracted"
        assert (out / record["json_recovery"]).name == "json_recovery.json"
        assert record["json_recovery"] in (out / "report.md").read_text(
            encoding="utf-8"
        )
    assert json.loads((out / "report.json").read_text(encoding="utf-8")) == report


def test_recovered_false_still_stops_at_hard_gate(setup):
    task, _, _, reviewer, _ = setup
    task.stages = task.stages[:1]
    task.stages[0].criteria[0].hard_gate = True
    original = reviewer.fn

    def respond(call):
        reply = original(call)
        if call.phase == "evaluation":
            data = json.loads(reply.text)
            data["criteria"][0]["passed"] = False
            # Single-quote syntax repair must preserve boolean false without flipping verdicts.
            return Reply(json.dumps(data).replace('"', "'"))
        return reply

    reviewer.fn = respond
    report = run(setup)
    assert report["status"] == "failed"
    turn = report["stages"][0]["turns"][0]
    assert turn["failed_hard_gates"] == ["explanation"]
    assert turn["evaluation"][1]["passed"] is False
    assert "json_recovery" in turn["calls"][-1]


@pytest.mark.parametrize("problem", ["missing", "duplicate", "unknown"])
def test_recovered_review_still_requires_exact_check_ids(setup, problem):
    task, _, _, reviewer, _ = setup
    task.stages = task.stages[:1]
    original = reviewer.fn

    def respond(call):
        reply = original(call)
        if call.phase == "evaluation":
            data = json.loads(reply.text)
            match problem:
                case "missing":
                    data["criteria"] = []
                case "duplicate":
                    data["criteria"] *= 2
                case "unknown":
                    data["criteria"][0]["id"] = "unknown"
            return Reply("Evaluation:\n" + json.dumps(data))
        return reply

    reviewer.fn = respond
    report = run(setup)
    assert report["status"] == "error"
    turn = report["stages"][0]["turns"][0]
    assert turn["error_details"]["phase"] == "evaluation"
    assert "json_recovery" in turn["calls"][-1] and "evaluation" not in turn


def test_recovered_progress_cannot_select_unknown_continuation(setup):
    task, _, _, reviewer, _ = setup
    task.stages = task.stages[:1]
    reviewer.fn = lambda call: Reply(
        'Decision:\n{"reason": "Needs confirmation", "decision": "request_user_reply", "continuation_id": "unknown"}'
    )
    report = run(setup)
    assert report["status"] == "error"
    turn = report["stages"][0]["turns"][0]
    assert turn["error_details"]["phase"] == "progress_review"
    assert "json_recovery" in turn["calls"][-1] and "evaluation_id" not in turn


def test_schema_failure_preserves_recovery_but_does_not_advance(setup):
    task, _, _, reviewer, out = setup
    task.stages = task.stages[:1]
    raw = 'Decision:\n{"reason":"Delivered","decision":"review_submission","continuation_id":null,"extra":true}'
    reviewer.fn = lambda call: Reply(raw)
    report = run(setup)
    assert report["status"] == "error"
    turn = report["stages"][0]["turns"][0]
    assert turn["error_details"]["phase"] == "progress_review"
    record = turn["calls"][-1]
    audit = json.loads((out / record["json_recovery"]).read_text(encoding="utf-8"))
    assert audit["value"]["extra"] is True
    assert "evaluation" not in turn and "assistant" in turn
    assert (
        json.loads((out / record["log_dir"] / "reply.json").read_text())["text"] == raw
    )


def test_verifier_prose_is_still_protocol_error_without_recovery(setup):
    task = setup[0]
    task.stages = task.stages[:1]
    script = task.root / "verify.py"
    script.write_text(
        'print("Prose output is invalid in programmatic verifier stdout")\n' + script.read_text(), encoding="utf-8"
    )
    report = run(setup)
    assert report["status"] == "error"
    turn = report["stages"][0]["turns"][0]
    assert turn["error_details"]["phase"] == "verification"
    assert "verification" not in turn and "review" not in turn
    assert all("json_recovery" not in call for call in turn["calls"])


@pytest.mark.parametrize("raw", ['Note\n{"message": "result"}', "{'message': 'result'}"])
def test_strict_parser_remains_strict(raw):
    with pytest.raises(OutputProtocolError):
        parse_and_validate_json(raw, SAMPLE_SCHEMA)


@pytest.mark.parametrize(
    "raw, expected_method",
    [
        ('[user\'s note] result: {"message": "pass"}', "extracted"),
        ('[note: \'draft] result: {"message": "pass"}', "extracted"),
        ("user's note: {'message': 'pass'}", "repaired"),
    ],
)
def test_apostrophe_in_prose_prefix_does_not_swallow_following_json(
    raw, expected_method
):
    data, recovery = parse_model_json(raw)
    assert validate_json(data, SAMPLE_SCHEMA) == {"message": "pass"}
    assert recovery["method"] == expected_method


@pytest.mark.parametrize(
    "raw",
    [
        '[user\'s note] {"message": "first"}\n{"message": "second"}',
        '[user\'s note] {"message": "complete"}\n{"message":',
        "[user's note] no JSON object yet",
    ],
)
def test_apostrophe_prefix_still_rejects_multiple_or_unclosed_json(raw):
    with pytest.raises(OutputProtocolError):
        data, _ = parse_model_json(raw)
        validate_json(data, SAMPLE_SCHEMA)
