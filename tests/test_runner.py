"""Verify Runner stage scheduling semantics with deterministic role doubles and no network calls."""

import json
from dataclasses import asdict
from pathlib import Path
import subprocess
import sys

import pytest

from multi_round.models import UserMessage, Action, Check, Criterion, Hook, Reply, Stage, Task, load_task
from multi_round.runner import Runner
from multi_round.prompts import RETRY_MESSAGE, RETRY_MESSAGE_ZH, user_prompt


class FakeAgent:
    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def run(self, call):
        self.calls.append(call)
        return self.fn(call)


def context(prompt):
    return json.loads(prompt[prompt.index("\n{\n") + 1:])


def review_conversation(call):
    filename = call.workspace / context(call.prompt)["conversation"]["path"]
    return [json.loads(line) for line in filename.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("verification", ["", "HIDDEN_METHOD: Compare reply against actual result.txt"])
def test_criterion_instructions_reach_review_and_snapshot_only(setup, verification):
    task, user, worker, reviewer, out = setup
    task.stages = task.stages[:1]
    check = task.stages[0].criteria[0]
    check.verification = verification
    # Round-trip through JSON loading so new fields are verified across both constructors.
    definition = {**asdict(task), "root": str(task.root)}
    path = task.root / "definition.json"
    path.write_text(json.dumps(definition), encoding="utf-8")
    task = load_task(path)
    result = Runner(user, worker, reviewer, progress=lambda _: None).run(task, out)
    assert result["status"] == "completed"
    formal = [call for call in reviewer.calls if context(call.prompt)["phase"] == "evaluation"]
    assert context(formal[0].prompt)["stage"]["criteria"][0] == asdict(check)
    saved = json.loads((out / "task.json").read_text(encoding="utf-8"))
    assert saved["stages"][0]["criteria"][0] == asdict(check)
    assert saved["stages"][0]["verifier_checks"] == [
        {"id": "artifact", "description": "Valid artifact", "hard_gate": True}
    ]
    progress = [call for call in reviewer.calls if context(call.prompt)["phase"] == "progress"]
    for call in user.calls + worker.calls + progress:
        assert "HIDDEN_METHOD" not in call.prompt


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "task"
    root.mkdir()
    for i in range(1, 4):
        (root / f"material{i}.txt").write_text(f"Round {i} material", encoding="utf-8")
    stages = [Stage(f"s{i}", f"Stage {i}", Action(f"a{i}", UserMessage(instruction=f"Complete round {i}"), [f"material{i}.txt"]),
                    "Actual submission", [Check("artifact", "Valid artifact")],
                    [Criterion("explanation", "HIDDEN_CRITERION", False)]) for i in range(1, 4)]
    # Programmatic verifier reads the real workspace file; writing 'bad' triggers a hard-gate failure.
    script = root / "verify.py"
    script.write_text('''import json
from pathlib import Path
passed = Path("result.txt").read_text() != "bad"
print(json.dumps({"checks":[{"id":"artifact","passed":passed,"evidence":"Read result.txt"}],"observations":{}}))
''', encoding="utf-8")
    task = Task("test", "Test Task", "Standard User", "Test background", stages,
                [sys.executable, str(script)], root)
    user = FakeAgent(lambda c: Reply(context(c.prompt)["current_action"]["message"]["instruction"]))

    def work(c):
        (c.workspace / "result.txt").write_text("good", encoding="utf-8")
        return Reply("Done", c.session_id or "worker-session")

    def review(c):
        if context(c.prompt)["phase"] == "progress":
            assert c.workspace is None  # Never pass an empty dummy directory for workspace-free calls.
            assert not (c.log_dir.parent / "submission").exists()
            return Reply(json.dumps({"reason": "Worker reported delivery", "decision": "review_submission",
                                     "continuation_id": None}))
        assert (c.log_dir.parent / "submission").is_dir()
        assert (c.log_dir.parent / "verifier/result.json").is_file()
        assert context(c.prompt)["verifier_results"]["checks"]
        result_file = c.workspace / context(c.prompt)["verifier_results"]["path"]
        assert json.loads(result_file.read_text(encoding="utf-8")) == json.loads(
            (c.log_dir.parent / "verifier/result.json").read_text(encoding="utf-8")
        )
        assert review_conversation(c)[-1]["role"] == "assistant"
        return Reply(json.dumps({"reason": "Evaluated submission and programmatic checks",
                                 "criteria": [{"id": "explanation", "passed": True, "evidence": "Reply explained changes"}]}))

    return task, user, FakeAgent(work), FakeAgent(review), tmp_path / "run"


def run(setup, *, reviewer_protocol_retry_limit=0):
    task, user, worker, reviewer, out = setup
    return Runner(
        user, worker, reviewer, progress=lambda _: None,
        reviewer_protocol_retry_limit=reviewer_protocol_retry_limit,
    ).run(task, out, "test")


def test_fixed_message_preserves_text_attachments_and_preparation(setup):
    task, user, worker, reviewer, out = setup
    literal = '  Please keep "literal quotes".\nRate: 6%.\n'
    task.stages = task.stages[:1]
    task.stages[0].initial.message = UserMessage(type="fixed", text=literal)
    task.hooks = [Hook("mark", "before_user", [sys.executable, "-c",
                      "from pathlib import Path; Path('hook.txt').write_text('ready')"],
                      "Fixed messages still trigger before_user hooks", stage_id="s1"),
                  Hook("prepare", "before_user", description="Task preparation", stage_id="s1",
                       handler="prepare")]
    events = []

    def prepare(event):
        assert event["stage_id"] == "s1"
        assert (out / "workspace/hook.txt").read_text(encoding="utf-8") == "ready"
        assert (out / "workspace/material1.txt").is_file()
        events.append(Path(event["turn_dir"]).name)

    report = Runner(user, worker, reviewer, hook_handlers={"prepare": prepare},
                    progress=lambda _: None).run(task, out)
    assert report["status"] == "completed"
    assert not user.calls
    assert events == ["s1-01"]
    assert [h["status"] for h in report["hooks"]] == ["completed", "completed"]
    assert (out / "user_workspace/material1.txt").is_file()
    assert worker.calls[0].prompt == literal
    assert len(worker.calls[0].attachments) == 1
    assert report["stages"][0]["turns"][0]["user_message_type"] == "fixed"
    assert not (out / "turns/s1-01/user").exists()
    saved = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert saved["stages"][0]["turns"][0]["user"] == literal
    definition = json.loads((out / "task.json").read_text(encoding="utf-8"))
    assert definition["stages"][0]["initial"]["message"]["text"] == literal


def test_context_message_receives_conversation_but_no_workspace_or_images(setup):
    task, user, worker, _, out = setup
    # Image attachments reach the Worker, whereas a context-mode User receives metadata only.
    task.stages[0].initial.attachments[0].kind = "image"
    original = user.fn

    def respond(call):
        data = context(call.prompt)
        assert call.workspace is None and call.attachments == []
        assert data["current_action"]["message"]["type"] == "context"
        if len(user.calls) == 2:
            assert data["latest_worker_reply"]["content"] == "Done"
            assert "material1.txt" in data["visible_files"]
        return original(call)

    user.fn = respond
    assert run(setup)["status"] == "completed"
    assert worker.calls[0].attachments[0].kind == "image"
    assert (out / "user_workspace/material1.txt").is_file()


def test_user_receives_latest_reply_separately_from_answered_history(setup):
    _, user, worker, _, _ = setup
    old_question = "Should we reprice the September invoice?"
    current_question = "Updated the price. Should the three omitted invoices join September?"
    original_worker = worker.fn

    def work(call):
        reply = original_worker(call)
        reply.text = [old_question, current_question, "Done"][len(worker.calls) - 1]
        return reply

    worker.fn = work
    seen = []

    def respond(call):
        data = context(call.prompt)
        match len(seen):
            case 0:
                assert data["latest_worker_reply"] is None
                assert data["earlier_conversation"] == []
                message = "Check the prices first"
            case 1:
                assert data["latest_worker_reply"]["content"] == old_question
                message = "Use the updated price for the September invoice"
            case 2:
                assert data["latest_worker_reply"] == {"role": "assistant", "content": current_question}
                assert data["earlier_conversation"][1]["content"] == old_question
                assert data["earlier_conversation"][2]["content"] == "Use the updated price for the September invoice"
                assert current_question not in str(data["earlier_conversation"])
                message = "Use your own judgment per the contract and finalize"
        seen.append(message)
        return Reply(message)

    # Runner separates latest_worker_reply from earlier_conversation so prior questions are not re-answered.
    user.fn = respond
    result = run(setup)
    assert result["status"] == "completed"
    assert worker.calls[2].prompt == seen[2]
    assert worker.calls[2].history[-1]["content"] == current_question


@pytest.mark.parametrize("value", [
    {"type": "fixed", "text": ""},
    {"type": "fixed", "text": "literal", "instruction": "rewrite"},
    {"type": "context", "text": "literal", "instruction": "generate"},
    {"type": "workspace"},
    {"type": "unknown", "instruction": "generate"},
])
def test_invalid_message_configuration_rejected(value):
    with pytest.raises(ValueError):
        Action("ask", value)


def test_dialogue_only_task_needs_no_file_verifier(tmp_path):
    definition = {"id": "clarify", "title": "Clarify", "persona": "User", "background": "Background",
                  "stages": [{"id": "s1", "title": "Ask about window",
                              "initial": {"id": "ask", "message": {"type": "context", "instruction": "Handle ambiguous request"}},
                              "checkpoint": "Clarifies time window", "needs_workspace": False,
                              "retry_limit": 0, "verifier_checks": [],
                              "criteria": [{"id": "question", "description": "Asks about time window"}]}]}
    path = tmp_path / "task.json"
    path.write_text(json.dumps(definition), encoding="utf-8")
    task = load_task(path)
    assert task.verifier == []
    assert task.stages[0].needs_workspace is False
    definition["stages"][0]["retry_limit"] = 1
    path.write_text(json.dumps(definition), encoding="utf-8")
    assert load_task(path).stages[0].retry_limit == 1  # Uses standard retry feedback without custom per-task prompt.


def test_three_stages_and_information_release(setup):
    task, user, worker, reviewer, out = setup
    original = worker.fn
    seen = []

    def work(c):
        seen.append(sorted(p.name for p in c.workspace.glob("material*")))
        assert "HIDDEN_CRITERION" not in c.prompt
        return original(c)

    worker.fn = work
    result = run(setup)
    assert result["status"] == "completed"
    assert [s["status"] for s in result["stages"]] == ["passed"] * 3
    assert seen == [["material1.txt"], ["material1.txt", "material2.txt"],
                    ["material1.txt", "material2.txt", "material3.txt"]]
    assert worker.calls[1].session_id == "worker-session"
    assert [c.prompt for c in worker.calls] == ["Complete round 1", "Complete round 2", "Complete round 3"]
    assert [len(c.history) for c in worker.calls] == [0, 2, 4]
    assert worker.calls[1].history == [{"role": "user", "content": "Complete round 1",
                                       "attachments": [{"path": "material1.txt", "kind": "file"}]},
                                     {"role": "assistant", "content": "Done"}]
    assert all("HIDDEN_CRITERION" not in json.dumps(c.history) for c in worker.calls)
    assert all(c.session_id is None for c in reviewer.calls + user.calls)
    assert [context(c.prompt)["phase"] for c in reviewer.calls] == ["progress", "evaluation"] * 3
    assert all("HIDDEN_CRITERION" not in c.prompt for c in user.calls)
    assert (out / "report.md").is_file()
    assert len(result["stages"][2]["turns"][0]["evaluation"]) == 2
    assert "trajectory_review" not in result
    assert not (out / "trajectory").exists()


def test_program_failure_cannot_be_overridden(setup):
    _, _, worker, _, _ = setup
    worker.fn = lambda c: ((c.workspace / "result.txt").write_text("bad", encoding="utf-8"), Reply("Done"))[1]
    result = run(setup)
    assert result["status"] == "failed"
    assert [s["status"] for s in result["stages"]] == ["failed", "pending", "pending"]
    assert len(worker.calls) == 1


@pytest.mark.parametrize("phase,invalid_reply", [
    ("user", ""),
    ("progress", "not-json"),
    ("progress", '{"unexpected":true}'),
    ("reviewer", "not-json"),
    ("reviewer", '{"unexpected":true}'),
])
def test_role_format_error_stops_without_retry_and_preserves_previous_stage(
    setup, phase, invalid_reply
):
    task, user, worker, reviewer, out = setup
    task.stages[1].retry_limit = 2  # Worker retry budget must never mask controller-role output protocol errors.
    agent = user if phase == "user" else reviewer
    original = agent.fn
    failures = []

    def respond(call):
        if call.log_dir.parent.name == "s2-01" and call.log_dir.name == phase:
            failures.append(call)
            # User replies require non-empty plain text; mutate Reply.text after init to simulate bad adapter output.
            reply = Reply("temporary text")
            reply.text = invalid_reply
            return reply
        return original(call)

    agent.fn = respond
    report = run(setup)
    assert report["status"] == "error"
    assert [s["status"] for s in report["stages"]] == ["passed", "error", "pending"]
    assert report["retry_count"] == 0
    assert len(failures) == 1
    assert len(user.calls) == 2
    assert len(worker.calls) == (1 if phase == "user" else 2)
    assert len(reviewer.calls) == {"user": 2, "progress": 3, "reviewer": 4}[phase]
    assert report["stages"][0]["turns"][0]["evaluation"]
    turn = report["stages"][1]["turns"][0]
    assert "error" in turn and "evaluation" not in turn
    assert "failed_hard_gates" not in turn
    saved = json.loads((failures[0].log_dir / "reply.json").read_text(encoding="utf-8"))
    assert saved["text"] == invalid_reply
    assert not (out / "workspace/material3.txt").exists()


def test_worker_receives_generated_message_not_user_instruction(setup):
    task, user, worker, _, _ = setup
    task.stages[0].initial.message.instruction = "Ask the Worker to organize the materials naturally in character."
    original = user.fn

    def respond(call):
        if context(call.prompt)["current_action"]["id"] == "a1":
            assert task.stages[0].initial.message.instruction in call.prompt
            return Reply("Could you organize this material for me?")
        return original(call)

    user.fn = respond
    report = run(setup)
    assert report["status"] == "completed"
    assert worker.calls[0].prompt == "Could you organize this material for me?"
    assert report["stages"][0]["turns"][0]["user"] == worker.calls[0].prompt
    assert task.stages[0].initial.message.instruction not in json.dumps(worker.calls[1].history, ensure_ascii=False)


@pytest.mark.parametrize("message", [
    "  Rate is 6%.\nKeep line breaks.\n",
    'Run with this config:\n```json\n{"region":"N"}\n```',
    '{"message":"Literal JSON body from user, not an envelope"}',
])
def test_generated_user_text_is_forwarded_without_json_parsing(setup, message):
    task, user, worker, _, out = setup
    task.stages = task.stages[:1]
    user.fn = lambda call: Reply(message)
    report = run(setup)
    assert report["status"] == "completed"
    assert user.calls[0].schema is None
    assert worker.calls[0].prompt == message
    turn = report["stages"][0]["turns"][0]
    assert turn["user"] == message
    assert turn["user_attachments"][0]["path"] == "material1.txt"
    assert context(user.calls[0].prompt)["current_attachments"] == [
        {"path": "material1.txt", "kind": "file"}
    ]
    record = turn["calls"][0]
    assert "json_recovery" not in record
    assert json.loads((out / record["log_dir"] / "reply.json").read_text(encoding="utf-8"))["text"] == message


@pytest.mark.parametrize("example,script", [
    ("excel_revision", "tests/fixtures/excel_revision/demo_agent.py"),
    ("file_collaboration", "tests/fixtures/file_collaboration/demo_agent.py"),
])
def test_demo_user_uses_separate_message_fixtures(example, script):
    project = Path(__file__).resolve().parents[1]
    task = load_task(project / "tests/fixtures" / example / "task.json")
    fixtures = json.loads((task.root / "demo_user_messages.json").read_text(encoding="utf-8"))
    for stage in task.stages:
        for action in [stage.initial, *stage.continuations]:
            if action.message.type == "fixed":
                assert action.message.text == fixtures[action.id]
                continue
            request = {"role": "user", "prompt": user_prompt(task, stage, action, [])}
            result = subprocess.run([sys.executable, str(project / script)],
                                    input=json.dumps(request), text=True, capture_output=True, check=True, encoding="utf-8")
            message = json.loads(result.stdout)["text"]
            assert message == fixtures[action.id]
            assert message != action.message.instruction


@pytest.mark.parametrize("retry_limit,max_turns", [(0, 3), (1, 3), (2, 1)])
@pytest.mark.parametrize("check_type", ["program", "semantic"])
def test_quality_failure_advances_without_retry(setup, retry_limit, max_turns, check_type):
    setup[0].stages[0].retry_limit = retry_limit
    setup[0].stages[0].max_turns = max_turns
    reviewer = setup[3]
    original = reviewer.fn

    def review(c):
        data = json.loads(original(c).text)
        if check_type == "semantic" and context(c.prompt)["phase"] == "evaluation":
            data["criteria"][0]["passed"] = False
        return Reply(json.dumps(data))

    reviewer.fn = review
    if check_type == "program":
        for stage in setup[0].stages:
            stage.verifier_checks[0].hard_gate = False
        setup[2].fn = lambda c: ((c.workspace / "result.txt").write_text("bad", encoding="utf-8"), Reply("Done"))[1]
    result = run(setup)
    assert result["status"] == "completed"
    first = result["stages"][0]
    assert len(first["turns"]) == 1
    assert first["retry_count"] == result["retry_count"] == 0
    assert first["status"] == "advanced_with_errors"
    evaluation = first["turns"][0]["evaluation"]
    failed = [c for c in evaluation if not c["passed"]]
    assert [c["id"] for c in failed] == ["artifact" if check_type == "program" else "explanation"]
    assert all(not c["hard_gate"] for c in failed)
    assert first["turns"][0]["failed_hard_gates"] == []
    assert "retry_unavailable" not in first["turns"][0]
    assert [c.prompt for c in setup[2].calls] == ["Complete round 1", "Complete round 2", "Complete round 3"]
    assert all(s["status"] == "advanced_with_errors" for s in result["stages"])


def test_semantic_hard_gate_blocks(setup):
    setup[0].stages[0].criteria[0].hard_gate = True
    reviewer = setup[3]
    original = reviewer.fn

    def review(c):
        data = json.loads(original(c).text)
        if context(c.prompt)["phase"] == "evaluation":
            data["criteria"][0]["passed"] = False
        return Reply(json.dumps(data))

    reviewer.fn = review
    assert run(setup)["status"] == "failed"


def test_dialogue_stage_is_judged_without_file_freeze(setup):
    task, _, worker, reviewer, out = setup
    task.stages[0] = Stage("s1", "Clarify fiscal month", Action("ask", UserMessage(instruction="Organize data by fiscal month")),
                           "Asks targeted fiscal-month boundary question", [],
                           [Criterion("targeted_question", "Explicitly asks for fiscal-month start and end dates")],
                           needs_workspace=False)
    original_work, original_review = worker.fn, reviewer.fn

    def work(c):
        if c.prompt == "Organize data by fiscal month":
            return Reply("Which exact start and end dates define the fiscal month?", "worker-session")
        return original_work(c)

    def review(c):
        data = context(c.prompt)
        if data["stage"]["id"] == "s1" and data["phase"] == "evaluation":
            assert c.workspace == c.log_dir.parent / "review"
            assert not (c.workspace / "submission").exists()
            assert data["verifier_results"] is None
            assert "start and end dates" in review_conversation(c)[-1]["content"]
            assert not (c.log_dir.parent / "submission").exists()
            return Reply(json.dumps({"reason": "Asked required boundary question", "criteria": [
                {"id": "targeted_question", "passed": True, "evidence": "Asked for start and end dates"}]}))
        return original_review(c)

    worker.fn, reviewer.fn = work, review
    result = run(setup)
    assert result["status"] == "completed"
    first = result["stages"][0]["turns"][0]
    assert first["evaluation"][0]["passed"] is True
    assert "snapshot" not in first and "verification" not in first
    assert (out / first["review_workspace"] / "evaluation/conversation.jsonl").is_file()
    assert not (out / first["review_workspace"] / "submission").exists()


@pytest.mark.parametrize("quality_failure", [False, True])
def test_hard_gate_retry_stays_in_same_stage(setup, quality_failure):
    stage = setup[0].stages[0]
    stage.retry_limit = 2
    stage.max_turns = 3
    worker = setup[2]
    original = worker.fn

    def work(c):
        if len(worker.calls) == 1:
            (c.workspace / "result.txt").write_text("bad", encoding="utf-8")
            return Reply("Done", "worker-session")
        return original(c)

    worker.fn = work
    if quality_failure:
        reviewer = setup[3]
        original_review = reviewer.fn

        def review(c):
            data = json.loads(original_review(c).text)
            if context(c.prompt)["phase"] == "evaluation":
                data["criteria"][0]["passed"] = False
            return Reply(json.dumps(data))

        reviewer.fn = review
    result = run(setup)
    assert result["status"] == "completed"
    attempts = result["stages"][0]["turns"]
    # Once hard gates pass, non-gate quality failures advance immediately even with retries remaining.
    expected_outcome = "advanced_with_errors" if quality_failure else "passed"
    assert [t["outcome"] for t in attempts] == ["retry", expected_outcome]
    assert (setup[4] / attempts[0]["snapshot"] / "result.txt").read_text(encoding="utf-8") == "bad"
    assert (setup[4] / attempts[1]["snapshot"] / "result.txt").read_text(encoding="utf-8") == "good"
    assert setup[2].calls[1].prompt == RETRY_MESSAGE
    assert len(setup[1].calls) == 3  # Retries do not invoke the User role.
    assert "HIDDEN_CRITERION" not in setup[1].calls[1].prompt
    assert [t["retry_count"] for t in attempts] == [0, 1]
    assert result["retry_count"] == result["stages"][0]["retry_count"] == 1
    assert [s["retry_count"] for s in result["stages"]] == [1, 0, 0]
    evaluations = [context(c.prompt) for c in setup[3].calls if context(c.prompt)["phase"] == "evaluation"]
    assert evaluations[1]["attempt"] == {"stage_turn": 2, "retry_count": 1, "stage_start_message": 0}
    assert evaluations[2]["attempt"]["stage_start_message"] == 4


def test_hard_gate_retry_exhaustion_stops_before_next_stage(setup):
    stage = setup[0].stages[0]
    stage.retry_limit = 1
    stage.max_turns = 2
    setup[2].fn = lambda c: ((c.workspace / "result.txt").write_text("bad", encoding="utf-8"), Reply("Done"))[1]
    result = run(setup)
    assert result["status"] == "failed"
    assert [t["outcome"] for t in result["stages"][0]["turns"]] == ["retry", "failed"]
    assert result["stages"][1]["status"] == "pending"
    assert result["retry_count"] == 1


def add_continuation(setup):
    task, _, worker, reviewer, _ = setup
    task.stages[0].continuations = [Action("answer", UserMessage(instruction="This is the confirmed version"), when="Asks about material version")]
    original_work = worker.fn
    work_calls = 0

    def work(c):
        nonlocal work_calls
        work_calls += 1
        if work_calls == 1:
            return Reply("Please confirm the material version first", "worker-session")
        return original_work(c)

    worker.fn = work
    original = reviewer.fn

    def review(c):
        if len(reviewer.calls) == 1 and context(c.prompt)["phase"] == "progress":
            return Reply(json.dumps({"reason": "Asking to confirm material version", "decision": "request_user_reply",
                                     "continuation_id": "answer"}))
        return original(c)

    reviewer.fn = review


def test_two_turns_stay_in_one_stage(setup):
    add_continuation(setup)
    result = run(setup)
    assert result["status"] == "completed"
    assert [len(s["turns"]) for s in result["stages"]] == [2, 1, 1]
    second = setup[4] / result["stages"][0]["turns"][1]["snapshot"]
    assert not (second / "material2.txt").exists()
    assert "evaluation" not in result["stages"][0]["turns"][0]
    assert "verification" not in result["stages"][0]["turns"][0]
    assert "snapshot" not in result["stages"][0]["turns"][0]
    assert result["retry_count"] == 0
    assert [t["retry_count"] for t in result["stages"][0]["turns"]] == [0, 0]


def test_clarification_without_file_does_not_run_verifier(setup):
    add_continuation(setup)
    result = run(setup)
    assert result["status"] == "completed"
    first = result["stages"][0]["turns"][0]
    assert first["progress"]["decision"] == "request_user_reply"
    assert "verification" not in first
    assert "snapshot" not in first
    assert "review" not in first
    assert not (setup[4] / "turns/s1-01/submission").exists()
    assert not (setup[4] / "turns/s1-01/review").exists()
    assert not (setup[4] / "turns/s1-01/verifier").exists()


def test_claimed_completion_with_missing_file_is_checked_and_fails(setup):
    task = setup[0]
    task.verifier = [sys.executable, "-c", 'import json; from pathlib import Path; '
                     'print(json.dumps({"checks":[{"id":"artifact","passed":'
                     'Path("result.txt").is_file(),"evidence":"Checked whether result.txt exists"}],'
                     '"observations":{}}))']
    setup[2].fn = lambda c: Reply("Done; saved to result.txt")
    result = run(setup)
    assert result["status"] == "failed"
    assert result["stages"][0]["turns"][0]["verification"]["checks"][0]["passed"] is False
    assert result["stages"][1]["status"] == "pending"


def test_budget_exhaustion_not_silent_advance(setup):
    add_continuation(setup)
    setup[0].stages[0].max_turns = 1
    result = run(setup)
    assert result["status"] == "budget_exhausted"
    assert result["stages"][1]["status"] == "pending"


@pytest.mark.parametrize("change", [
    lambda d: d.update(criteria=[]),
    lambda d: d["criteria"].append(dict(d["criteria"][0])),
    lambda d: d["criteria"][0].update(passed="true"),
])
def test_invalid_judgment_is_error_not_model_failure(setup, change):
    reviewer = setup[3]
    original = reviewer.fn

    def review(c):
        data = json.loads(original(c).text)
        if context(c.prompt)["phase"] == "evaluation":
            change(data)
        return Reply(json.dumps(data))

    reviewer.fn = review
    result = run(setup)
    assert result["status"] == "error"
    assert result["stages"][1]["status"] == "pending"


def test_invalid_progress_cannot_advance(setup):
    reviewer = setup[3]
    original = reviewer.fn

    def review(c):
        if context(c.prompt)["phase"] == "progress":
            return Reply(json.dumps({"reason": "Invalid action", "decision": "request_user_reply",
                                     "continuation_id": "invented"}))
        return original(c)

    reviewer.fn = review
    result = run(setup)
    assert result["status"] == "error"
    assert "snapshot" not in result["stages"][0]["turns"][0]


def test_review_uses_separate_copy_and_preserves_submission(setup):
    reviewer = setup[3]
    original = reviewer.fn

    def review(c):
        if context(c.prompt)["phase"] == "evaluation":
            submitted = c.log_dir.parent / "submission"
            assert c.workspace == c.log_dir.parent / "review"
            assert not (c.workspace / "submission/result.txt").samefile(submitted / "result.txt")
            assert (c.workspace / "submission/result.txt").read_text(encoding="utf-8") == "good"
        return original(c)

    reviewer.fn = review
    result = run(setup)
    assert result["status"] == "completed"
    # Subsequent workspace edits do not alter frozen submission or review snapshots.
    (setup[4] / "workspace/result.txt").write_text("later revision", encoding="utf-8")
    first = result["stages"][0]["turns"][0]
    assert (setup[4] / first["snapshot"] / "result.txt").read_text(encoding="utf-8") == "good"
    assert (setup[4] / "turns/s1-01/review/submission/result.txt").read_text(encoding="utf-8") == "good"


def test_verifier_crash_preserves_failed_attempt(setup):
    setup[0].stages[0].retry_limit = 2
    setup[0].verifier = [sys.executable, "-c", "raise RuntimeError('broken checker')"]
    result = run(setup)
    assert result["status"] == "error"
    assert "assistant" in result["stages"][0]["turns"][0]
    assert len(setup[3].calls) == 1  # Progress review routed to submission, then verifier crashed before rubric review.
    assert result["retry_count"] == 0  # Verifier crashes do not spend Worker retry budget.


@pytest.mark.parametrize("workspace_stages", [set(), {"s1", "s2", "s3"}, {"s2"}])
def test_progress_and_evaluation_routing_is_independent_of_workspace(setup, workspace_stages):
    task, user, worker, reviewer, out = setup
    original = reviewer.fn
    for stage in task.stages:
        stage.needs_workspace = stage.id in workspace_stages
        if not stage.needs_workspace:
            stage.verifier_checks = []

    def formal_review(c):
        data = context(c.prompt)
        assert data["phase"] == "evaluation"
        needs_workspace = data["stage"]["id"] in workspace_stages
        assert c.workspace == c.log_dir.parent / "review"
        assert (c.workspace / "submission").exists() == needs_workspace
        assert (c.log_dir.parent / "submission").exists() == needs_workspace
        assert review_conversation(c)[-1]["role"] == "assistant"
        assert "HIDDEN_CRITERION" in c.prompt
        if needs_workspace:
            return original(c)
        assert data["verifier_results"] is None
        return Reply(json.dumps({"reason": "Dialogue review", "criteria": [
            {"id": "explanation", "passed": True, "evidence": "Explained update"}
        ]}))

    def text_review(c):
        assert c.workspace is None
        assert context(c.prompt)["phase"] == "progress"
        assert "HIDDEN_CRITERION" not in c.prompt
        assert not c.attachments
        return original(c)

    reviewer.fn = formal_review
    progress_reviewer = FakeAgent(text_review)
    report = Runner(user, worker, reviewer, progress_reviewer=progress_reviewer,
                    progress=lambda _: None).run(task, out)
    assert report["status"] == "completed"
    assert len(progress_reviewer.calls) == len(reviewer.calls) == 3
    assert not list(out.glob("turns/*/progress_workspace"))


def test_retry_not_counted_when_environment_preparation_fails(setup):
    task, user, worker, reviewer, out = setup
    task.stages[0].retry_limit = 1
    worker.fn = lambda c: ((c.workspace / "result.txt").write_text("bad", encoding="utf-8"), Reply("Done"))[1]

    def prepare(event):
        if event["turn"] == 2:
            raise RuntimeError("environment preparation failed")

    task.hooks = [Hook("prepare", "before_user", description="Task preparation", stage_id="s1",
                       handler="prepare")]
    report = Runner(user, worker, reviewer, hook_handlers={"prepare": prepare},
                    progress=lambda _: None).run(task, out)
    assert report["status"] == "error"
    assert report["retry_count"] == report["stages"][0]["retry_count"] == 0
    assert len(worker.calls) == len(user.calls) == 1
    assert report["stages"][0]["turns"][0]["outcome"] == "retry"
    assert "retry_count" not in report["stages"][0]["turns"][1]
    assert [h["status"] for h in report["hooks"]] == ["completed", "error"]
    assert "environment preparation failed" in report["hooks"][-1]["error"]


def test_unregistered_hook_stops_before_user_and_runs_cleanup(setup):
    task, user, worker, reviewer, out = setup
    task.hooks = [
        Hook("missing", "before_user", description="Unbound hook handler", stage_id="s1", handler="missing"),
        Hook("cleanup", "after_run", description="Cleanup environment", handler="cleanup"),
    ]
    cleaned = []
    report = Runner(user, worker, reviewer, hook_handlers={"cleanup": lambda event: cleaned.append(event)},
                    progress=lambda _: None).run(task, out)
    assert report["status"] == "error"
    assert not user.calls and not worker.calls
    assert "Unregistered hook handler" in report["reason"]
    assert [h["status"] for h in report["hooks"]] == ["error", "completed"]
    assert cleaned[0]["run_status"] == "error" and cleaned[0]["turn_dir"] is None
    assert json.loads((out / "report.json").read_text(encoding="utf-8")) == report


def test_callback_cleanup_failure_does_not_skip_command_cleanup(setup):
    task, user, worker, reviewer, out = setup
    task.hooks = [
        Hook("first", "after_run", description="Failing cleanup", handler="fail"),
        Hook("second", "after_run", [sys.executable, "-c", "from pathlib import Path; Path('cleaned').touch()"],
             "Still clean up"),
    ]
    def fail(event):
        raise RuntimeError("callback cleanup failed")
    report = Runner(user, worker, reviewer, hook_handlers={"fail": fail},
                    progress=lambda _: None).run(task, out)
    assert report["status"] == "error" and "callback cleanup failed" in report["cleanup_error"]
    assert [h["status"] for h in report["hooks"]] == ["error", "completed"]
    assert (out / "workspace/cleaned").exists()


def test_retry_has_no_room_in_turn_budget(setup):
    setup[0].stages[0].retry_limit = 2
    setup[0].stages[0].max_turns = 1
    setup[2].fn = lambda c: ((c.workspace / "result.txt").write_text("bad", encoding="utf-8"), Reply("Done"))[1]
    report = run(setup)
    assert report["status"] == "failed"
    assert report["retry_count"] == 0
    assert report["stages"][0]["turns"][0]["retry_unavailable"] == "max_turns"


def test_clarification_during_retry_does_not_spend_another_retry(setup):
    task, _, worker, reviewer, _ = setup
    task.stages[0].retry_limit = 1
    task.stages[0].continuations = [Action("version", UserMessage(instruction="This is the final version"), when="Asks version")]
    original_work, original_review = worker.fn, reviewer.fn

    def work(c):
        if len(worker.calls) == 1:
            (c.workspace / "result.txt").write_text("bad", encoding="utf-8")
            return Reply("Done", "worker-session")
        if len(worker.calls) == 2:
            return Reply("Is this the final version?", "worker-session")
        return original_work(c)

    def review(c):
        data = context(c.prompt)
        if data["phase"] == "progress" and len(worker.calls) == 2:
            return Reply(json.dumps({"reason": "Confirming version", "decision": "request_user_reply",
                                     "continuation_id": "version"}))
        return original_review(c)

    worker.fn, reviewer.fn = work, review
    report = run(setup)
    assert report["status"] == "completed"
    turns = report["stages"][0]["turns"]
    assert [t["retry_count"] for t in turns] == [0, 1, 1]
    assert report["retry_count"] == 1
    assert "evaluation" not in turns[1]
    assert turns[2]["outcome"] == "passed"


def test_dialogue_retry_preserves_first_attempt_and_timing(setup):
    task, _, worker, reviewer, out = setup
    task.stages = [Stage("s1", "Clarify", Action("ask", UserMessage(instruction="Organize data by fiscal month")),
                        "Asks fiscal-month boundary", [], [Criterion("question", "Clarifies fiscal-month boundary")],
                        needs_workspace=False, retry_limit=1)]
    worker.fn = lambda c: Reply("Done" if len(worker.calls) == 1 else "Which day does the fiscal month start?")

    def review(c):
        data = context(c.prompt)
        if data["phase"] == "progress":
            assert c.workspace is None
            return Reply(json.dumps({"reason": "Evaluate reply including missed clarification",
                                     "decision": "review_submission", "continuation_id": None}))
        retried = data["attempt"]["retry_count"] == 1
        if retried:
            assert review_conversation(c)[1]["content"] == "Done"
            assert data["attempt"]["stage_turn"] == 2
        return Reply(json.dumps({"reason": "Evaluated current attempt while preserving prior failure", "criteria": [
            {"id": "question", "passed": retried, "evidence": "Whether boundary question was asked"}]}))

    reviewer.fn = review
    report = run(setup)
    assert report["status"] == "completed"
    turns = report["stages"][0]["turns"]
    assert [t["evaluation"][0]["passed"] for t in turns] == [False, True]
    assert [t["retry_count"] for t in turns] == [0, 1]
    assert not list(out.glob("turns/*/submission"))
    assert not list(out.glob("turns/*/verifier"))
    first = out / turns[0]["review_workspace"] / "evaluation/conversation.jsonl"
    second = out / turns[1]["review_workspace"] / "evaluation/conversation.jsonl"
    assert first != second
    assert len(first.read_text(encoding="utf-8").splitlines()) == 2
    assert len(second.read_text(encoding="utf-8").splitlines()) == 4
    assert "Which day does the fiscal month start" not in first.read_text(encoding="utf-8")


@pytest.mark.parametrize("checks", [
    [],
    [{"id": "artifact", "passed": True, "evidence": "Checked file"}] * 2,
    [{"id": "artifact", "passed": "false", "evidence": "Checked file"}],
])
def test_invalid_verifier_result_stops_before_advance(setup, checks):
    # Malformed verifier results must halt before invoking semantic evaluation or advancing stages.
    data = json.dumps({"checks": checks, "observations": {}})
    setup[0].verifier = [sys.executable, "-c", "print(" + repr(data) + ")"]
    result = run(setup)
    assert result["status"] == "error"
    assert result["stages"][1]["status"] == "pending"
    assert len(setup[3].calls) == 1


def test_symlink_submission_rejected(setup, tmp_path):
    worker = setup[2]
    worker.fn = lambda c: ((c.workspace / "outside").symlink_to(tmp_path), Reply("Done"))[1]
    assert run(setup)["status"] == "error"


def test_existing_run_not_overwritten(setup):
    run(setup)
    with pytest.raises(FileExistsError):
        run(setup)


def test_no_session_id_does_not_trigger_outer_history_concatenation(setup):
    worker = setup[2]
    original = worker.fn

    def work(c):
        reply = original(c)
        return Reply(reply.text)  # Stateless backends receive structured history separately.

    worker.fn = work
    assert run(setup)["status"] == "completed"
    assert worker.calls[1].prompt == "Complete round 2"
    assert len(worker.calls[1].history) == 2
    assert worker.calls[1].session_id is None


def test_adapter_cannot_mutate_recorded_conversation(setup):
    worker = setup[2]
    original = worker.fn

    def work(c):
        if c.history:
            assert c.history[0]["content"] == "Complete round 1"
            c.history[0]["content"] = "Temporary adapter mutation"
        return original(c)

    worker.fn = work
    result = run(setup)
    assert result["status"] == "completed"
    assert result["stages"][0]["turns"][0]["user"] == "Complete round 1"
    assert "Temporary adapter mutation" not in setup[3].calls[-1].prompt


@pytest.mark.parametrize("delete_attachment", [False, True])
def test_repeated_reply_does_not_republish_attachment(setup, delete_attachment):
    task, _, worker, reviewer, _ = setup
    (task.root / "extra.txt").write_text("Initial release", encoding="utf-8")
    task.stages[0].continuations = [Action("answer", UserMessage(instruction="This is the final version"), ["extra.txt"], "Confirm version")]
    original_work, original_review = worker.fn, reviewer.fn

    def work(c):
        if len(worker.calls) == 1:
            return Reply("Please confirm version", "session")
        if len(worker.calls) == 2:
            path = c.workspace / "extra.txt"
            assert path.read_text(encoding="utf-8") == "Initial release"
            if delete_attachment:
                path.unlink()
            else:
                path.write_text("Edited by Worker", encoding="utf-8")
            return Reply("Please confirm version again", "session")
        if len(worker.calls) == 3:
            if delete_attachment:
                assert not (c.workspace / "extra.txt").exists()
            else:
                assert (c.workspace / "extra.txt").read_text(encoding="utf-8") == "Edited by Worker"
        return original_work(c)

    def review(c):
        data = context(c.prompt)
        if data["phase"] == "progress" and len(worker.calls) <= 2:
            assert data["stage"]["continuations"][0]["id"] == "answer"
            return Reply(json.dumps({"reason": "Asking to confirm version", "decision": "request_user_reply",
                                     "continuation_id": "answer"}))
        return original_review(c)

    worker.fn, reviewer.fn = work, review
    report = run(setup)
    assert report["status"] == "completed"
    turns = report["stages"][0]["turns"]
    assert [t["action_id"] for t in turns] == ["a1", "answer", "answer"]
    assert [t["released_attachments"] for t in turns] == [["material1.txt"], ["extra.txt"], []]
    assert report["retry_count"] == 0


@pytest.mark.parametrize("keeps_asking", [False, True])
def test_unmatched_question_uses_default_reply_within_budget(setup, keeps_asking):
    task, user, worker, reviewer, _ = setup
    task.stages[0].default_reply = UserMessage(instruction="I have no extra details on that point and cannot confirm it for you.")
    task.stages[0].max_turns = 2
    original = reviewer.fn

    def review(c):
        if context(c.prompt)["phase"] == "progress" and (keeps_asking or len(worker.calls) == 1):
            return Reply(json.dumps({"reason": "No continuation matched", "decision": "request_user_reply",
                                     "continuation_id": None}))
        return original(c)

    reviewer.fn = review
    report = run(setup)
    assert report["status"] == ("budget_exhausted" if keeps_asking else "completed")
    second = report["stages"][0]["turns"][1]
    assert second["action_id"] == "default_reply"
    assert second["user"] == task.stages[0].default_reply.instruction
    assert second["released_attachments"] == []
    assert report["retry_count"] == 0
    assert "HIDDEN_CRITERION" not in user.calls[1].prompt
    if keeps_asking:
        assert len(worker.calls) == 2
        assert report["stages"][1]["status"] == "pending"


@pytest.mark.parametrize("stop", ["hard_gate", "budget", "verifier_error", "user_error", "interrupt"])
def test_early_stop_saves_stage_results_without_extra_evaluation(setup, stop, monkeypatch):
    original_hooks = Runner.run_hooks
    persisted_before_cleanup = []

    def check_saved_turns(self, task, event, run_dir, report, **kwargs):
        if event == "after_run":
            # Early-stop or error turns must already be flushed to report.json before cleanup hooks run.
            saved = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
            assert saved["stages"][0]["turns"] == report["stages"][0]["turns"]
            persisted_before_cleanup.append(True)
        return original_hooks(self, task, event, run_dir, report, **kwargs)

    monkeypatch.setattr(Runner, "run_hooks", check_saved_turns)
    if stop == "hard_gate":
        setup[2].fn = lambda c: ((c.workspace / "result.txt").write_text("bad", encoding="utf-8"), Reply("Done"))[1]
    elif stop == "budget":
        add_continuation(setup)
        setup[0].stages[0].max_turns = 1
    elif stop == "verifier_error":
        setup[0].verifier = [sys.executable, "-c", "raise RuntimeError('broken verifier')"]
    else:
        def fail(c):
            if stop == "interrupt":
                raise KeyboardInterrupt()
            raise RuntimeError("User model returned no message")

        setup[1].fn = fail

    report = run(setup)
    assert persisted_before_cleanup == [True]
    assert report["status"] == {"hard_gate": "failed", "budget": "budget_exhausted"}.get(stop, "error")
    assert report["stages"][1]["status"] == "pending"
    expected_phases = {"hard_gate": ["progress", "evaluation"],
                       "budget": ["progress"], "verifier_error": ["progress"]}.get(stop, [])
    assert [context(c.prompt)["phase"] for c in setup[3].calls] == expected_phases
    assert "trajectory_review" not in report
    assert not (setup[4] / "trajectory").exists()
    saved = json.loads((setup[4] / "report.json").read_text(encoding="utf-8"))
    assert saved == report


def test_execution_error_and_cleanup_notes_are_both_saved(setup):
    def fail(call):
        error = RuntimeError("execution failed")
        error.add_note("cleanup failed: simulated container removal error")
        raise error

    setup[2].fn = fail
    report = run(setup)
    turn = report["stages"][0]["turns"][0]
    assert turn["error"] == "RuntimeError: execution failed"
    assert turn["error_notes"] == ["cleanup failed: simulated container removal error"]
    assert json.loads((setup[4] / "report.json").read_text(encoding="utf-8")) == report
    rendered = (setup[4] / "report.md").read_text(encoding="utf-8")
    assert "execution failed" in rendered and "cleanup failed: simulated container removal error" in rendered


def test_task_loads_default_reply(tmp_path):
    original = Path(__file__).resolve().parent / "fixtures/excel_revision/task.json"
    task = load_task(original)
    assert task.stages[0].default_reply
    definition = {"id": "t", "title": "t", "persona": "u", "background": "b",
                  "stages": [{"id": "s1", "title": "s1", "initial": {"id": "ask", "message": {"type": "context", "instruction": "Ask"}},
                              "checkpoint": "Reply", "needs_workspace": False, "default_reply": {"type": "context", "instruction": "Not specified"},
                              "verifier_checks": [], "criteria": [{"id": "c", "description": "Check"}]}]}
    path = tmp_path / "task.json"
    path.write_text(json.dumps(definition), encoding="utf-8")
    assert load_task(path).stages[0].default_reply.instruction == "Not specified"


def test_reply_retry_reply_preserves_stage_session_and_verification_order(setup, monkeypatch):
    """Alternating continuations and retries advance turns within the same stage until verification passes."""
    task, user, worker, reviewer, out = setup
    task.stages[0].max_turns = 4
    task.stages[0].retry_limit = 1
    task.stages[0].continuations = [Action("answer", UserMessage(instruction="This is the final version"), when="Confirm version")]
    events = []
    original_user, original_review, original_verify = user.fn, reviewer.fn, Runner.verify
    original_hooks = Runner.run_hooks
    persisted_before_next_turn = []

    def check_saved_turns(self, task, event, run_dir, report, **kwargs):
        if event == "before_user":
            # Prior completed turns must be flushed to report.json before the next turn's hooks or calls run.
            previous = [turn for stage in report["stages"] for turn in stage["turns"]][:-1]
            if previous:
                saved = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
                saved_turns = [turn for stage in saved["stages"] for turn in stage["turns"]]
                assert saved_turns == previous
            else:
                assert not (run_dir / "report.json").exists()  # Do not write an empty report before turn 1.
            persisted_before_next_turn.append(len(previous))
        return original_hooks(self, task, event, run_dir, report, **kwargs)

    def respond(c):
        events.append("user")
        return original_user(c)

    def work(c):
        events.append("worker")
        call_no = len(worker.calls)
        assert len(c.history) == 2 * (call_no - 1)
        assert c.session_id == (None if call_no == 1 else "session")
        if call_no == 1:
            (c.workspace / "state.txt").write_text("workspace preserved", encoding="utf-8")
        else:
            assert (c.workspace / "state.txt").read_text(encoding="utf-8") == "workspace preserved"
        if call_no <= 4:
            assert {p.name for p in c.workspace.glob("material*")} == {"material1.txt"}
        if call_no in (1, 3):
            return Reply("Please confirm version", "session")
        (c.workspace / "result.txt").write_text("bad" if call_no == 2 else "good", encoding="utf-8")
        return Reply("Done", "session")

    def review(c):
        phase = context(c.prompt)["phase"]
        events.append(phase)
        if phase == "progress" and len(worker.calls) in (1, 3):
            assert c.workspace is None
            assert not (c.log_dir.parent / "submission").exists()
            return Reply(json.dumps({"reason": "Asking to confirm version", "decision": "request_user_reply",
                                     "continuation_id": "answer"}))
        return original_review(c)

    def verify(self, *args, **kwargs):
        events.append("verifier")
        return original_verify(self, *args, **kwargs)

    user.fn, worker.fn, reviewer.fn = respond, work, review
    monkeypatch.setattr(Runner, "verify", verify)
    monkeypatch.setattr(Runner, "run_hooks", check_saved_turns)
    report = run(setup)
    assert report["status"] == "completed"
    assert persisted_before_next_turn == list(range(6))
    turns = report["stages"][0]["turns"]
    assert [t["action_id"] for t in turns] == ["a1", "answer", "retry_1", "answer"]
    assert [t["retry_count"] for t in turns] == [0, 0, 1, 1]
    assert [t.get("outcome") for t in turns] == [None, "retry", None, "passed"]
    assert [len(s["turns"]) for s in report["stages"]] == [4, 1, 1]
    assert report["retry_count"] == 1
    assert [c.prompt for c in worker.calls] == [
        "Complete round 1", "This is the final version", RETRY_MESSAGE, "This is the final version", "Complete round 2", "Complete round 3",
    ]
    assert events == (
        ["user", "worker", "progress"]
        + ["user", "worker", "progress", "verifier", "evaluation"]
        + ["worker", "progress"]  # Fixed retry prompt bypasses the User role.
        + ["user", "worker", "progress", "verifier", "evaluation"] * 3
    )
    assert (out / turns[1]["snapshot"] / "result.txt").read_text(encoding="utf-8") == "bad"
    assert (out / turns[3]["snapshot"] / "result.txt").read_text(encoding="utf-8") == "good"


def test_progress_failure_keeps_reply_files_without_submitting(setup):
    task, _, _, reviewer, out = setup
    task.stages = task.stages[:1]
    def stop(call):
        assert context(call.prompt)['phase'] == 'progress'
        assert not (out/'user_workspace/result.txt').exists()
        raise RuntimeError('injected progress failure')
    reviewer.fn = stop
    result = run(setup)
    turn = result['stages'][0]['turns'][0]
    assert result['status'] == 'error' and 'snapshot' not in turn and 'evaluation_id' not in turn
    assert (out/turn['reply_snapshot']/'result.txt').read_text() == 'good'
    assert 'result.txt' in json.loads((out/turn['reply_snapshot']).with_name('intermediate_manifest.json').read_text())
    assert not (out/'user_workspace/result.txt').exists()


def test_progress_prompt_hides_untriggered_user_replies_and_delivers_after_trigger(setup):
    task, user, worker, reviewer, _ = setup
    task.stages = task.stages[:1]
    stage = task.stages[0]
    stage.max_turns = 2
    secret_reply = "SECRET_REPLY_BODY: budget cap is 48200"
    secret_default = "SECRET_DEFAULT_REPLY: no additional offline passcode"
    (task.root / "secret_extra.txt").write_text("secret", encoding="utf-8")
    stage.continuations = [
        Action(
            "budget_rule",
            UserMessage(type="context", instruction=secret_reply),
            ["secret_extra.txt"],
            when="Worker asks about the budget cap",
        )
    ]
    stage.default_reply = UserMessage(type="fixed", text=secret_default)
    original_work, original_review = worker.fn, reviewer.fn
    progress_prompts = []

    def work(call):
        if len(worker.calls) == 1:
            return Reply("What is the budget cap?", "session")
        return original_work(call)

    def review(call):
        data = context(call.prompt)
        if data["phase"] == "progress":
            progress_prompts.append(call.prompt)
            assert data["stage"]["continuations"] == [
                {"id": "budget_rule", "when": "Worker asks about the budget cap"}
            ]
            assert "default_reply" not in data["stage"]
            assert secret_default not in call.prompt
            stage_json = json.dumps(data["stage"], ensure_ascii=False)
            assert secret_reply not in stage_json and "secret_extra.txt" not in stage_json
            if len(worker.calls) == 1:
                assert secret_reply not in call.prompt
                assert "secret_extra.txt" not in call.prompt
                return Reply(
                    json.dumps(
                        {
                            "reason": "Asked about budget cap",
                            "decision": "request_user_reply",
                            "continuation_id": "budget_rule",
                        }
                    )
                )
        return original_review(call)

    worker.fn, reviewer.fn = work, review
    report = run(setup)
    assert report["status"] == "completed", report["reason"]
    assert len(progress_prompts) == 2
    assert len(user.calls) == 2
    assert secret_reply not in user.calls[0].prompt
    assert secret_reply in user.calls[1].prompt


def test_retry_message_language_priority_and_filename_immunity(setup):
    from multi_round.prompts import resolve_public_language, retry_message_for

    # 1. English prose citing a Chinese filename stays English.
    en_with_zh_file = [{"role": "user", "content": "Please review Q3_对账单.xlsx and update the summary memo."}]
    assert resolve_public_language(None, en_with_zh_file) == "en"
    assert retry_message_for(None, en_with_zh_file) == RETRY_MESSAGE

    # 2. Chinese prose citing English codes/filenames resolves to Chinese.
    zh_with_en_code = [{"role": "user", "content": "请核对 SKU-1010 和 FY26_Merit_Workbook.xlsx 的 variance。"}]
    assert resolve_public_language(None, zh_with_en_code) == "zh"
    assert retry_message_for(None, zh_with_en_code) == RETRY_MESSAGE_ZH

    # 3. Chinese user prompt dispatches RETRY_MESSAGE_ZH automatically; explicit public_language="en" overrides it.
    task, user, worker, reviewer, out = setup
    task.stages = task.stages[:1]
    stage = task.stages[0]
    stage.initial.message = UserMessage(type="fixed", text="请完成第一轮交付。")
    stage.retry_limit = 1
    stage.max_turns = 2
    calls = 0

    def work(c):
        nonlocal calls
        calls += 1
        (c.workspace / "result.txt").write_text("bad" if calls in (1, 3) else "good", encoding="utf-8")
        return Reply("done")

    worker.fn = work
    zh_report = Runner(user, worker, reviewer, progress=lambda _: None).run(task, out / "zh_auto")
    assert zh_report["status"] == "completed"
    assert worker.calls[1].prompt == RETRY_MESSAGE_ZH

    en_report = Runner(user, worker, reviewer, progress=lambda _: None, public_language="en").run(task, out / "en_override")
    assert en_report["status"] == "completed"
    assert worker.calls[3].prompt == RETRY_MESSAGE
