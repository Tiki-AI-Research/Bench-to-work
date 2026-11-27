"""Integration tests for file/image attachments and workspace hooks, including reproducible failure variants."""

from dataclasses import asdict
import json
from pathlib import Path
import sys

import pytest

from multi_round.agents.command import CommandAgent
from multi_round.models import UserMessage, Action, Attachment, Criterion, Hook, Reply, Stage, Task, load_task
from multi_round.runner import Runner


EXAMPLE = Path(__file__).resolve().parent / "fixtures/file_collaboration"


@pytest.mark.parametrize("variant,failed_stage,failed_check", [
    ("happy", None, None),
    ("missed_clarification", "s1", "clarify_scope"),
    ("stale_user_edit", "s2", "latest_user_edit"),
    ("ignore_image", "s2", "image_applied"),
    ("ignore_lock", "s3", "ask_before_retry"),
])
def test_example_paths(tmp_path, variant, failed_stage, failed_check):
    task = load_task(EXAMPLE / "task.json")
    command = [sys.executable, str(EXAMPLE / "demo_agent.py")]
    user = CommandAgent(command, supports_text_only=True)
    worker = CommandAgent(command + ["--variant", variant])
    reviewer = CommandAgent(command, supports_text_only=True)
    out = tmp_path / "run"
    report = Runner(user, worker, reviewer, progress=lambda _: None).run(task, out, "test")
    assert report["status"] == ("completed" if failed_stage is None else "failed")
    if failed_stage:
        stage = next(s for s in report["stages"] if s["id"] == failed_stage)
        assert failed_check in stage["turns"][-1]["failed_hard_gates"]
        assert all(not s["turns"] for s in report["stages"] if s["id"] > failed_stage)
    assert report["hooks"][-1]["id"] == "release_on_exit"
    assert report["hooks"][-1]["status"] == "completed"
    assert (out / "workspace/shared/report.json").stat().st_mode & 0o200
    assert "trajectory_review" not in report
    assert not (out / "trajectory").exists()
    first = out / "turns/s1-01"
    assert not (first / "submission").exists()  # Clarification-only stage is evaluated via rubric without snapshot.
    first_call = json.loads((first / "worker/request.json").read_text(encoding="utf-8"))
    assert [a["kind"] for a in first_call["attachments"]] == ["file", "file", "image"]
    assert all(Path(a["path"]).is_absolute() for a in first_call["attachments"])
    progress = json.loads((first / "progress/request.json").read_text(encoding="utf-8"))
    assert progress["workspace"] is None and progress["attachments"] == []
    if variant == "happy":
        assert [len(s["turns"]) for s in report["stages"]] == [1, 1, 2]
        assert report["retry_count"] == 0  # Requesting lock release is normal dialogue, not a failed-gate retry.
        assert (out / "workspace/lock_observed.txt").read_text(encoding="utf-8") == "PermissionError"
        assert [e["id"] for e in report["hooks"]] == [
            "user_edit", "user_open", "user_close", "release_on_exit"]
        stage2 = report["stages"][1]["turns"][0]
        first_file = report["stages"][0]["turns"][0]["user_attachments"][0]
        assert json.loads((out / first_file["saved_path"]).read_text(encoding="utf-8"))["note"] == "Initial note"
        assert json.loads((out / stage2["snapshot"] / "shared/report.json").read_text(encoding="utf-8"))["title"] == "September Review"
        assert json.loads((out / "workspace/shared/report.json").read_text(encoding="utf-8"))["title"] == "September Review (Confirmed)"
        assert (out / stage2["snapshot"] / "output/preview.png").is_file()
        assert not list(out.glob("turns/*/attachments/assistant"))
        assert all("assistant_attachments" not in t for s in report["stages"] for t in s["turns"])
        assert len(json.loads((out / "turns/s3-02/reviewer/request.json").read_text(encoding="utf-8"))["attachments"]) == 1
        assert "!" in (out / "report.md").read_text(encoding="utf-8")


class FunctionAgent:
    def __init__(self, fn):
        self.fn, self.calls = fn, []

    def run(self, call):
        self.calls.append(call)
        return self.fn(call)


def make_runner_task(tmp_path, hooks):
    stage = Stage("s1", "Reply", Action("start", UserMessage(instruction="Please reply")), "Review on reply", [],
                  [Criterion("reply", "Replies as requested")], needs_workspace=False)
    task = Task("test", "Test Task", "User", "Background", [stage], [], tmp_path, hooks=hooks)
    user = FunctionAgent(lambda c: Reply("Please reply"))
    worker = FunctionAgent(lambda c: Reply("Done", "session"))

    def judge(call):
        data = json.loads(call.prompt[call.prompt.index("\n{\n") + 1:])
        if data["phase"] == "progress":
            assert call.workspace is None and not call.attachments
            assert "HOOK_PRIVATE" not in call.prompt
            return Reply('{"reason":"Replied","decision":"review_submission","continuation_id":null}')
        assert isinstance(data["environment_events"], list)
        return Reply('{"reason":"Replied","criteria":[{"id":"reply","passed":true,"evidence":"Done"}]}')

    reviewer = FunctionAgent(judge)
    return task, Runner(user, worker, reviewer, progress=lambda _: None), worker, reviewer


def command_hook(id, event, code, **kwargs):
    return Hook(id, event, [sys.executable, "-c", code], "HOOK_PRIVATE", **kwargs)


def test_user_reads_submitted_version_not_live_workspace_or_private_files(tmp_path):
    task_root = tmp_path / "task"
    task_root.mkdir()
    (task_root / "input.txt").write_text("Published material", encoding="utf-8")
    (task_root / "future.txt").write_text("Future material", encoding="utf-8")
    (task_root / "answer.txt").write_text("HIDDEN_ANSWER", encoding="utf-8")
    stages = [Stage(f"s{i}", f"Stage {i}", Action(f"a{i}", UserMessage(type="workspace", instruction="Read prior result before asking"),
                       ["input.txt"] if i == 1 else ["future.txt"]),
                    "Review on reply", [], [Criterion("done", "Completed")]) for i in (1, 2)]
    hook = command_hook("edit_unshared", "before_user",
                        "from pathlib import Path; Path('output/report.json').write_text('{\"amount\":999}')",
                        stage_id="s2", turn=1)
    task = Task("readback", "Readback", "User", "Background", stages, [], task_root,
                submission_paths=["output"], hooks=[hook])
    out = tmp_path / "run"

    def user(call):
        data = json.loads(call.prompt[call.prompt.index("\n{\n") + 1:])
        assert call.workspace == out / "user_workspace"
        assert "HIDDEN_ANSWER" not in call.prompt and "HOOK_PRIVATE" not in call.prompt
        assert not (call.workspace / "answer.txt").exists()
        assert not (call.workspace / "worker-private.txt").exists()
        if data["current_action"]["id"] == "a1":
            assert data["visible_files"] == ["input.txt"]
            assert not (call.workspace / "future.txt").exists()
            return Reply("Generate the summary first")
        assert json.loads((out / "workspace/output/report.json").read_text(encoding="utf-8"))["amount"] == 999
        amount = json.loads((call.workspace / "output/report.json").read_text(encoding="utf-8"))["amount"]
        assert amount == 47  # Subsequent workspace mutations must not overwrite the previously delivered snapshot.
        assert data["latest_worker_reply"]["role"] == "assistant"
        return Reply(f"Previous total was {amount}, please continue")

    def worker(call):
        (call.workspace / "output").mkdir(exist_ok=True)
        (call.workspace / "output/report.json").write_text('{"amount":47}', encoding="utf-8")
        (call.workspace / "worker-private.txt").write_text("Unsubmitted", encoding="utf-8")
        return Reply("Done", "session")  # Formal submission publishes declared paths to User; chat text carries no files.

    def reviewer(call):
        data = json.loads(call.prompt[call.prompt.index("\n{\n") + 1:])
        if data["phase"] == "progress":
            assert call.workspace is None
            return Reply('{"reason":"Replied","decision":"review_submission","continuation_id":null}')
        assert call.workspace.name == "review"
        assert not (call.workspace / "submission/input.txt").exists()
        assert not (call.workspace / "input.txt").exists()
        return Reply('{"reason":"Test double","criteria":[{"id":"done","passed":true,"evidence":"Done"}]}')

    report = Runner(FunctionAgent(user), FunctionAgent(worker), FunctionAgent(reviewer),
                    progress=lambda _: None).run(task, out)
    assert report["status"] == "completed"
    assert "47" in report["stages"][1]["turns"][0]["user"]
    assert not (out / "user_workspace/verifier").exists()


def test_missing_resubmitted_file_removes_old_user_copy(tmp_path):
    from multi_round.storage import receive_submission
    user_workspace = tmp_path / "user"
    (user_workspace / "output").mkdir(parents=True)
    (user_workspace / "output/report.json").write_text("Stale deliverable", encoding="utf-8")
    (user_workspace / "input.txt").write_text("Previously published material", encoding="utf-8")
    submitted = tmp_path / "submission"
    submitted.mkdir()
    receive_submission(submitted, user_workspace, ["output"])
    assert not (user_workspace / "output/report.json").exists()
    assert (user_workspace / "input.txt").read_text(encoding="utf-8") == "Previously published material"


def test_hook_order_and_information_isolation(tmp_path):
    hooks = [command_hook(id, event,
             f"from pathlib import Path; p=Path('events.txt'); p.write_text((p.read_text() if p.exists() else '')+'{id}\\n')",
             **({"stage_id": "s1"} if event != "after_run" else {}))
             for id, event in [("before", "before_user"), ("after", "after_worker"),
                               ("stage", "after_stage"), ("cleanup", "after_run")]]
    task, runner, worker, reviewer = make_runner_task(tmp_path, hooks)

    def work(call):
        assert (call.workspace / "events.txt").read_text(encoding="utf-8") == "before\n"
        assert "HOOK_PRIVATE" not in call.prompt
        return Reply("Done")

    worker.fn = work
    report = runner.run(task, tmp_path / "run")
    assert report["status"] == "completed"
    assert (tmp_path / "run/workspace/events.txt").read_text(encoding="utf-8") == "before\nafter\nstage\ncleanup\n"
    assert [e["id"] for e in report["hooks"]] == ["before", "after", "stage", "cleanup"]
    saved = json.loads((tmp_path / "run/report.json").read_text(encoding="utf-8"))
    assert saved == report
    for event in report["hooks"]:
        assert (tmp_path / "run" / event["log_dir"] / "request.json").is_file()
    # Stage evaluation sees only events that already occurred; after_stage has not fired yet.
    assert '"id": "after"' in reviewer.calls[-1].prompt
    assert '"id": "stage"' not in reviewer.calls[-1].prompt


@pytest.mark.parametrize(
    "scenario, status, events",
    [
        ("reply_then_retry", "completed",
         ["before_user", "after_worker"] * 3 + ["after_stage", "after_run"]),
        ("gate_failure", "failed", ["before_user", "after_worker", "after_run"]),
        ("budget", "budget_exhausted", ["before_user", "after_worker"] * 3 + ["after_run"]),
        ("worker_error", "error", ["before_user", "after_run"]),
    ],
)
def test_hook_lifecycles_across_turns_and_exits(tmp_path, scenario, status, events):
    hooks = [
        command_hook(event, event, "pass", stage_id=None if event == "after_run" else "s1")
        for event in ("before_user", "after_worker", "after_stage", "after_run")
    ]
    task, runner, worker, reviewer = make_runner_task(tmp_path, hooks)
    task.stages[0].max_turns = 3
    task.stages[0].retry_limit = 1 if scenario == "reply_then_retry" else 0

    def work(call):
        if scenario == "worker_error":
            raise RuntimeError("worker unavailable")
        return Reply("Please clarify" if len(worker.calls) == 1 else "Done", "session")

    def judge(call):
        data = json.loads(call.prompt[call.prompt.index("\n{\n") + 1:])
        if data["phase"] == "progress":
            needs_reply = scenario == "budget" or (
                scenario == "reply_then_retry" and len(worker.calls) == 1
            )
            return Reply(json.dumps({
                "reason": "Needs reply" if needs_reply else "Ready for review",
                "decision": "request_user_reply" if needs_reply else "review_submission",
                "continuation_id": None,
            }))
        return Reply(json.dumps({
            "reason": "Checked reply",
            "criteria": [{"id": "reply", "passed": len(worker.calls) == 3, "evidence": "Reply content"}],
        }))

    worker.fn, reviewer.fn = work, judge
    report = runner.run(task, tmp_path / "run")
    assert report["status"] == status
    assert [event["event"] for event in report["hooks"]] == events
    assert report["retry_count"] == (1 if scenario == "reply_then_retry" else 0)
    if scenario == "reply_then_retry":
        turns = report["stages"][0]["turns"]
        assert turns[1]["evaluation"][0]["passed"] is False
        assert turns[2]["evaluation"][0]["passed"] is True
    assert json.loads((tmp_path / "run/report.json").read_text(encoding="utf-8")) == report


@pytest.mark.parametrize("event", ["before_user", "after_worker", "after_stage"])
def test_hook_failure_stops_and_still_cleans_up(tmp_path, event):
    hooks = [command_hook("broken", event, "raise RuntimeError('broken hook')", stage_id="s1"),
             command_hook("cleanup", "after_run", "from pathlib import Path; Path('cleaned').touch()")]
    task, runner, worker, _ = make_runner_task(tmp_path, hooks)
    report = runner.run(task, tmp_path / "run")
    assert report["status"] == "error"
    assert [e["status"] for e in report["hooks"]] == ["error", "completed"]
    assert (tmp_path / "run/workspace/cleaned").exists()
    assert len(worker.calls) == (0 if event == "before_user" else 1)
    assert json.loads((tmp_path / "run/report.json").read_text(encoding="utf-8")) == report


def test_cleanup_failure_does_not_skip_other_cleanup(tmp_path):
    hooks = [command_hook("before", "before_user", "pass", stage_id="s1"),
             command_hook("broken", "after_run", "raise RuntimeError('cleanup failed')"),
             command_hook("cleanup", "after_run", "from pathlib import Path; Path('cleaned').touch()")]
    task, runner, _, _ = make_runner_task(tmp_path, hooks)
    report = runner.run(task, tmp_path / "run")
    assert report["status"] == "error" and report["cleanup_error"]
    assert report["stages"][0]["status"] == "passed"
    assert (tmp_path / "run/workspace/cleaned").exists()
    assert json.loads((tmp_path / "run/report.json").read_text(encoding="utf-8")) == report


def test_interrupt_still_releases_resources(tmp_path):
    hooks = [command_hook("cleanup", "after_run", "from pathlib import Path; Path('cleaned').touch()")]
    task, runner, worker, _ = make_runner_task(tmp_path, hooks)

    def interrupt(call):
        raise KeyboardInterrupt()

    worker.fn = interrupt
    report = runner.run(task, tmp_path / "run")
    assert report["interrupted"] is True
    assert (tmp_path / "run/workspace/cleaned").exists()


@pytest.mark.parametrize("change", [
    {"event": "unknown"}, {"turn": 0}, {"stage_id": "missing"},
    {"action_id": "missing"}, {"turn": 100}, {"command": "echo hi"},
])
def test_invalid_hook_definition_rejected(tmp_path, change):
    raw = json.loads((EXAMPLE / "task.json").read_text(encoding="utf-8"))
    # Keep only an attachment-free stage so missing fixture files do not mask hook schema validation.
    raw["stages"] = [raw["stages"][0]]
    raw["stages"][0]["initial"]["attachments"] = []
    raw["hooks"] = [{"id": "test", "event": "before_user", "stage_id": "s1", "turn": 1,
                     "command": ["true"], "description": "test", **change}]
    path = tmp_path / "task.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_task(path)


def test_attachment_compatibility_and_invalid_kind():
    action = Action("a", UserMessage(instruction="test"), ["a.txt", {"path": "b.png", "kind": "image"}])
    assert [asdict(a) for a in action.attachments] == [
        {"path": "a.txt", "kind": "file"}, {"path": "b.png", "kind": "image"}]
    with pytest.raises(ValueError, match="kind"):
        Attachment("a", "unknown")


def test_file_reference_without_submission_does_not_publish_workspace(tmp_path):
    task, runner, worker, reviewer = make_runner_task(tmp_path, [])

    def work(call):
        (call.workspace / "answer.txt").write_text("Deliverable content", encoding="utf-8")
        return Reply("Details are in answer.txt")

    worker.fn = work
    report = runner.run(task, tmp_path / "run")
    assert report["status"] == "completed"
    assert "answer.txt" in reviewer.calls[0].prompt
    assert not (tmp_path / "run/user_workspace/answer.txt").exists()
    assert not (tmp_path / "run/turns/s1-01/submission").exists()


def test_failed_user_generation_does_not_publish_unsent_image_evidence(tmp_path):
    task, runner, worker, _ = make_runner_task(tmp_path, [])
    (tmp_path / "new.png").write_bytes((EXAMPLE / "materials/accent.png").read_bytes())
    task.stages[0].initial.attachments = [Attachment("new.png", "image")]

    def fail(call):
        assert call.attachments  # User model sees staged image attachments, but fails before emitting a turn.
        raise RuntimeError("user generation failed")

    runner.user.fn = fail
    report = runner.run(task, tmp_path / "run")
    turn = report["stages"][0]["turns"][0]
    assert report["status"] == "error" and not worker.calls
    assert "user_attachments" not in turn


def test_configured_followup_attachments_do_not_require_hooks_or_user_json(tmp_path):
    task, runner, worker, reviewer = make_runner_task(tmp_path, [])
    (tmp_path / "supplement.txt").write_text("Supplemental material", encoding="utf-8")
    (tmp_path / "color.png").write_bytes((EXAMPLE / "materials/accent.png").read_bytes())
    stage = task.stages[0]
    stage.max_turns = 3
    stage.continuations = [Action(
        "send_materials", UserMessage(type="context", instruction="Send supplemental materials and ask Worker to continue"),
        [Attachment("supplement.txt"), Attachment("color.png", "image")],
        when="Worker requests supplemental materials",
    )]

    def user(call):
        data = json.loads(call.prompt[call.prompt.index("\n{\n") + 1:])
        assert call.schema is None and call.workspace is None
        expected = [asdict(a) for a in stage.continuations[0].attachments] if call.turn == 2 else []
        assert data["current_attachments"] == expected
        return Reply("Please continue with the attached materials.")

    def work(call):
        if call.turn == 1:
            assert not (call.workspace / "supplement.txt").exists()
        elif call.turn == 2:
            assert [a.kind for a in call.attachments] == ["file", "image"]
            (call.workspace / "supplement.txt").write_text("Worker edit", encoding="utf-8")
        else:
            assert call.attachments == []
            assert (call.workspace / "supplement.txt").read_text(encoding="utf-8") == "Worker edit"
        return Reply("Need supplemental materials" if call.turn < 3 else "Done", "session")

    original_review = reviewer.fn
    def judge(call):
        if call.phase == "progress_review" and call.turn < 3:
            return Reply('{"reason":"Requested materials","decision":"request_user_reply","continuation_id":"send_materials"}')
        return original_review(call)

    runner.user.fn, worker.fn, reviewer.fn = user, work, judge
    report = runner.run(task, tmp_path / "run")
    assert report["status"] == "completed" and report["hooks"] == []
    turns = report["stages"][0]["turns"]
    assert [len(t["user_attachments"]) for t in turns] == [0, 2, 0]
    archived = tmp_path / "run" / turns[1]["user_attachments"][0]["saved_path"]
    assert archived.read_text(encoding="utf-8") == "Supplemental material"
