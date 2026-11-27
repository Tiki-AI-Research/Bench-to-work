"""Tasks share identical validation constraints whether loaded from JSON or constructed in Python."""

import json
from dataclasses import asdict

import pytest

from multi_round.models import (
    Action,
    Check,
    Criterion,
    Hook,
    Stage,
    Task,
    UserMessage,
    load_task,
)


def test_reviewer_evidence_order_keeps_verifier_protocol_and_field_semantics():
    from multi_round.json_protocol import validate_json
    from multi_round.models import CHECK_SCHEMA, REVIEW_SCHEMA, VERIFIER_SCHEMA

    reviewer_item = REVIEW_SCHEMA["properties"]["criteria"]["items"]
    assert list(reviewer_item["properties"]) == ["id", "evidence", "passed"]
    assert list(CHECK_SCHEMA["properties"]) == ["id", "passed", "evidence"]
    assert VERIFIER_SCHEMA["properties"]["checks"]["items"] == CHECK_SCHEMA
    assert reviewer_item["properties"] == CHECK_SCHEMA["properties"]
    assert set(reviewer_item["required"]) == set(CHECK_SCHEMA["required"])
    # Property ordering guides generation, while arbitrary key ordering and boolean false stay valid.
    item = {"id": "check", "passed": False, "evidence": "Requirement not satisfied"}
    result = validate_json({"reason": "Verified", "criteria": [item]}, REVIEW_SCHEMA)
    assert result["criteria"][0]["passed"] is False


def dialogue_stage(**changes):
    fields = {
        "id": "s1",
        "title": "Clarify",
        "initial": Action("ask", UserMessage(instruction="Confirm scope")),
        "checkpoint": "Asks question",
        "verifier_checks": [],
        "criteria": [Criterion("ask", "Asks about scope")],
        "needs_workspace": False,
    }
    return Stage(**{**fields, **changes})


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"command": ["run"], "handler": "prepare"},
        {"handler": " "},
        {"handler": 1},
    ],
)
def test_hook_requires_exactly_one_execution_method(fields):
    with pytest.raises(ValueError):
        Hook("prepare", "before_user", description="Prepare environment", stage_id="s1", **fields)


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "../stage"},
        {"max_turns": 0},
        {"max_turns": True},
        {"retry_limit": -1},
        {"needs_workspace": "false"},
        {"criteria": []},
    ],
)
def test_stage_rules_apply_to_python_and_json(tmp_path, changes):
    with pytest.raises(ValueError):
        dialogue_stage(**changes)
    raw = {
        "id": "t",
        "title": "Task",
        "persona": "User",
        "background": "Background",
        "stages": [
            {
                "id": "s1",
                "title": "Clarify",
                "initial": {
                    "id": "ask",
                    "message": {"type": "context", "instruction": "Confirm scope"},
                },
                "checkpoint": "Asks question",
                "verifier_checks": [],
                "criteria": [{"id": "ask", "description": "Asks about scope"}],
                "needs_workspace": False,
                **changes,
            }
        ],
    }
    path = tmp_path / "task.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_task(path)


@pytest.mark.parametrize(
    "failure", ["duplicate_stage", "missing_verifier", "unknown_hook_stage"]
)
def test_python_task_checks_cross_object_constraints(tmp_path, failure):
    stages, hooks = [dialogue_stage()], []
    if failure == "duplicate_stage":
        stages.append(dialogue_stage())
    elif failure == "missing_verifier":
        stages = [
            dialogue_stage(
                needs_workspace=True, verifier_checks=[Check("file", "File exists")]
            )
        ]
    else:
        hooks = [
            Hook(
                "event",
                "before_user",
                ["example-command"],
                "Environment event",
                stage_id="missing",
            )
        ]
    with pytest.raises(ValueError):
        Task("t", "Task", "User", "Background", stages, [], tmp_path, hooks=hooks)


@pytest.mark.parametrize("invalid_id", [None, 3, " "])
def test_action_and_check_ids_are_nonempty_strings(invalid_id):
    with pytest.raises(ValueError):
        Action(invalid_id, UserMessage(instruction="Confirm scope"))
    with pytest.raises(ValueError):
        Criterion(invalid_id, "Check")
    with pytest.raises(ValueError):
        Check(invalid_id, "Check")


@pytest.mark.parametrize(
    "changes",
    [
        {"verification": None},
        {"verification": ["conversation"]},
        {"verification": 1},
        {"verification": {"tool": "read_file"}},
    ],
)
def test_check_instructions_are_text_in_python_and_json(tmp_path, changes):
    with pytest.raises(ValueError, match="must be a string"):
        Criterion("ask", "Asks about scope", **changes)
    stage = asdict(dialogue_stage())
    stage["criteria"][0].update(changes)
    raw = {
        "id": "t",
        "title": "Task",
        "persona": "User",
        "background": "Background",
        "stages": [stage],
    }
    path = tmp_path / "task.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="must be a string"):
        load_task(path)


def test_program_checks_do_not_accept_model_instructions(tmp_path):
    stage = asdict(dialogue_stage(needs_workspace=True))
    stage["verifier_checks"] = [
        {"id": "file", "description": "Deliverable file exists", "hard_gate": True}
    ]
    raw = {
        "id": "t",
        "title": "Task",
        "persona": "User",
        "background": "Background",
        "verifier": ["check-command"],
        "stages": [stage],
    }
    path = tmp_path / "task.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    task = load_task(path)
    assert asdict(task.stages[0].verifier_checks[0]) == stage["verifier_checks"][0]
    # Programmatic Check definitions must not silently accept ignored LLM verification instructions.
    stage["verifier_checks"][0]["verification"] = "Ask model to run extra check"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(TypeError, match="verification"):
        load_task(path)
