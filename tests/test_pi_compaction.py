"""Pi SDK compaction and raw evidence logging tests against a local protocol server."""

import json

import pytest
from test_pi import PROJECT
from test_pi import model_server as model_server
from test_pi import pi_agent as pi_agent

from multi_round.agents import make_agent
from multi_round.models import REVIEW_SCHEMA, Call


@pytest.mark.parametrize("enabled", [False, True])
def test_pi_compaction_preserves_raw_evidence_and_logs_summary(
    pi_agent, model_server, tmp_path, enabled
):
    pi_agent.config.update(
        context_window=4096,
        max_tokens=1024,
        max_model_turns=3,
        compaction={"enabled": enabled, "reserveTokens": 2048, "keepRecentTokens": 256},
    )
    evidence = "Worker reported 12000; expected 57180. " + "old-evidence " * 900
    (tmp_path / "earlier.txt").write_text(evidence, encoding="utf-8")
    (tmp_path / "latest.txt").write_text("Worker repeated 12000.", encoding="utf-8")
    summary = "Earlier evidence: Worker reported 12000, expected 57180; verify latest.txt next."
    reads = ["earlier.txt", "latest.txt"]
    summary_requests = []

    def respond(payload):
        # Compaction summary requests are independent of the final review schema and retry budget.
        if "response_format" not in payload:
            summary_requests.append(payload)
            return summary
        if reads:
            return {
                "name": "read",
                "arguments": json.dumps({"path": reads.pop(0)}),
            }
        return json.dumps(
            {
                "reason": "Submitted amount does not match expected figure",
                "criteria": [
                    {
                        "id": "amount",
                        "passed": False,
                        "evidence": "latest.txt: 12000 != 57180",
                    }
                ],
            }
        )

    model_server[2][0] = respond
    log_dir = tmp_path / "logs"
    reply = pi_agent.run(
        Call(
            "reviewer",
            "Read earlier.txt and latest.txt and compare actual values against expected.",
            tmp_path,
            log_dir,
            schema=REVIEW_SCHEMA,
        )
    )
    assert json.loads(reply.text)["criteria"][0]["passed"] is False
    events = [
        json.loads(line)
        for line in (log_dir / "stderr.log").read_text(encoding="utf-8").splitlines()
    ]
    config = next(event for event in events if event["type"] == "session_config")
    assert config["compaction"]["enabled"] is enabled
    # Raw tool execution outputs and workspace files remain inspectable even when context compacts.
    read_events = [event for event in events if event["type"] == "tool_execution_end"]
    assert evidence in json.dumps(read_events, ensure_ascii=False)
    assert (tmp_path / "earlier.txt").read_text(encoding="utf-8") == evidence
    compaction = [event for event in events if event["type"] == "compaction_end"]
    if enabled:
        assert summary_requests
        assert any(event["type"] == "compaction_start" for event in events)
        # Preserve any SDK-added turn-split header verbatim rather than post-processing summaries.
        assert compaction and all(
            summary in event["result"]["summary"] for event in compaction
        )
        assert (
            compaction[-1]["result"]["estimatedTokensAfter"]
            < compaction[-1]["result"]["tokensBefore"]
        )
        assert summary in json.dumps(model_server[1][-1]["messages"])
        assert all("response_format" not in request for request in summary_requests)
    else:
        assert not summary_requests and not compaction
        assert len(model_server[1]) == 3


@pytest.mark.parametrize(
    "compaction",
    [
        False,
        {"enabled": "true"},
        {"reserveTokens": 0},
        {"keepRecentTokens": True},
        {"unknown": 1},
    ],
)
def test_pi_rejects_invalid_compaction_config(compaction):
    with pytest.raises(ValueError, match="compaction"):
        make_agent(
            {
                "type": "pi",
                "base_url": "http://unused",
                "model": "fixture",
                "compaction": compaction,
            },
            PROJECT,
        )


def test_pi_default_compaction_is_disabled():
    agent = make_agent(
        {"type": "pi", "base_url": "http://unused", "model": "fixture"}, PROJECT
    )
    assert agent.config["compaction"] == {"enabled": False}
