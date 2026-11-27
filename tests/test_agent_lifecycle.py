"""Verify agent cleanup precedence vs. primary execution errors without duplicating run state."""

import json
from contextlib import ExitStack

import pytest

from multi_round.agents.lifecycle import close_agent, finish_agent_cleanup
from multi_round.records import save_report


@pytest.mark.parametrize("primary_failure", [False, True])
def test_all_agents_close_and_primary_error_is_preserved(tmp_path, primary_failure):
    report = {
        "title": "Cleanup Check",
        "mode": "test",
        "status": "failed",
        "reason": "Original hard-gate failure",
        "stages": [],
        "hooks": [],
    }
    save_report(tmp_path, report)
    errors, closed = [], []
    primary = ValueError("original execution failure")

    def close(name):
        closed.append(name)
        raise RuntimeError(f"{name} close failure")

    with pytest.raises(ValueError if primary_failure else RuntimeError) as caught:
        try:
            with ExitStack() as stack:
                for name in ("user", "reviewer"):
                    stack.callback(close_agent, lambda n=name: close(n), name, errors)
                if primary_failure:
                    raise primary
        finally:
            finish_agent_cleanup(errors, report, tmp_path)
    assert closed == ["reviewer", "user"]
    assert caught.value is (primary if primary_failure else errors[0][1])
    assert any("user close failure" in note for note in caught.value.__notes__)
    saved = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert saved["status"] == "error"
    assert "Original hard-gate failure" in saved["reason"]
    assert [e["role"] for e in saved["agent_cleanup_errors"]] == ["reviewer", "user"]


def test_cleanup_before_runner_does_not_rewrite_an_old_report(tmp_path):
    previous = tmp_path / "report.json"
    previous.write_text("previous run", encoding="utf-8")
    with pytest.raises(RuntimeError, match="close failure"):
        finish_agent_cleanup([("user", RuntimeError("close failure"))], None, tmp_path)
    assert previous.read_text(encoding="utf-8") == "previous run"


def test_cleanup_record_write_failure_does_not_replace_execution_error(
    tmp_path, monkeypatch
):
    def fail_save(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr("multi_round.agents.lifecycle.save_report", fail_save)
    report = {"status": "error", "reason": "original execution failure"}
    primary = ValueError("original execution failure")
    with pytest.raises(ValueError) as caught:
        try:
            raise primary
        finally:
            finish_agent_cleanup(
                [("user", RuntimeError("close failure"))], report, tmp_path
            )
    assert caught.value is primary
    assert any("disk unavailable" in note for note in primary.__notes__)
