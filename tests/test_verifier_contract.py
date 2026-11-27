"""Verifier crashes or non-boolean outputs are recorded as host errors rather than Worker failures."""

import json
import sys

import pytest

from test_runner import run
from test_runner import setup as setup


@pytest.mark.parametrize("hard_gate", [True, False])
@pytest.mark.parametrize("value", [None, "unknown", 1])
def test_non_boolean_verifier_result_stops_without_reward_or_retry(
    setup, hard_gate, value
):
    task, _, worker, reviewer, out = setup
    task.stages[0].verifier_checks[0].hard_gate = hard_gate
    task.stages[0].retry_limit = 2
    result = {
        "checks": [
            {"id": "artifact", "passed": value, "evidence": "Parser could not evaluate check"}
        ],
        "observations": {"reason": "unsupported"},
    }
    task.verifier = [
        sys.executable,
        "-c",
        f"print({json.dumps(result, ensure_ascii=False)!r})",
    ]
    report = run(setup)

    assert report["status"] == "error" and report["retry_count"] == 0
    turn = report["stages"][0]["turns"][0]
    assert turn["error_details"]["phase"] == "verification"
    assert turn["error_details"]["category"] == "output_protocol"
    assert turn["error_details"]["role"] is None
    assert "evaluation" not in turn and "failed_hard_gates" not in turn
    assert (
        len(worker.calls) == len(reviewer.calls) == 1
    )  # Only progress review ran; semantic rubric evaluation did not start.
    assert all(stage["status"] == "pending" for stage in report["stages"][1:])
    assert json.loads((out / "turns/s1-01/verifier/stdout.log").read_text()) == result
    assert not (out / "turns/s1-01/verifier/result.json").exists()


def test_verifier_execution_failure_keeps_reason_and_stops_without_retry(setup):
    task, _, worker, _, out = setup
    task.stages[0].retry_limit = 2
    task.verifier = [
        sys.executable,
        "-c",
        "raise RuntimeError('Verifier engine does not support this formula')",
    ]
    report = run(setup)
    turn = report["stages"][0]["turns"][0]

    assert report["status"] == "error" and report["retry_count"] == 0
    assert turn["error_details"]["phase"] == "verification"
    assert turn["error_details"]["category"] == "external_execution"
    assert turn["error_details"]["role"] is None and len(worker.calls) == 1
    assert "evaluation" not in turn and "failed_hard_gates" not in turn
    assert (
        "Verifier engine does not support this formula"
        in (out / "turns/s1-01/verifier/stderr.log").read_text()
    )
