"""Controller-side role wrappers for transient transport retries and reviewer verdict-consistency checks."""
from __future__ import annotations

import json
import re
import time
from dataclasses import replace
from pathlib import Path

from multi_round.agents.openai_model import OpenAICompatibleModel
from multi_round.json_protocol import parse_model_json
from multi_round.models import (
    AdapterError,
    ExternalExecutionError,
    IncompleteResponseError,
)

TRANSIENT = re.compile(
    r"finish_reason=(None|content_filter)"
    r"|connection failed|read timed out|was interrupted|invalid response"
    r"|连接失败|读取超时|传输中断|无效响应"
    r"|HTTP (429|5\d\d)"
)
LOGS = ("model_request.json", "events.jsonl", "response.json")


class RetryText:
    """Wrap OpenAICompatibleModel with bounded retries on transient gateway/stream failures."""

    def __init__(self, retries: int = 3, wait: float = 10, **text):
        self.model = OpenAICompatibleModel(**text)
        self.retries, self.wait = retries, wait

    def run(self, call):
        for n in range(1, self.retries + 2):
            try:
                return self.model.run(call)
            except AdapterError as exc:
                if n > self.retries or not TRANSIENT.search(str(exc)):
                    raise
                keep_attempt(Path(call.log_dir), n, exc)
                time.sleep(self.wait * n)

    def close(self):
        close = getattr(self.model, "close", None)
        if close:
            close()


def keep_attempt(log_dir: Path, n: int, exc: Exception) -> None:
    dst = log_dir / f"attempt-{n}"
    dst.mkdir(parents=True, exist_ok=True)
    for name in LOGS:
        if (log_dir / name).exists():
            (log_dir / name).replace(dst / name)
    (dst / "error.txt").write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")


def make_text(project, **text):
    return RetryText(**text)


EXPLICIT_VERDICT = re.compile(r"Verdict:\s*(PASS|FAIL)\.?\s*$")
SAYS_PASS_ZH = re.compile(
    r"(?:符合通过标准|满足通过|符合通过条件|应判通过|故通过|判通过|本项通过|，通过。|属标准认可|(?<!不)符合标准)[。.]?\s*$"
)
SAYS_FAIL_ZH = re.compile(
    r"(?<!未发现)(?<!没有)(?<!无)(?:不通过|不符合|不满足|未通过)[^。.!?\n]*[。.]?\s*$"
)


def contradictions(text: str) -> list[dict]:
    """Return items whose trailing evidence verdict contradicts their boolean `passed` field."""
    try:
        data, _ = parse_model_json(text)
    except Exception:  # noqa: BLE001 — malformed JSON is handled downstream by Runner protocol validation
        return []
    out = []
    for c in (data.get("criteria") if isinstance(data, dict) else None) or []:
        if not isinstance(c, dict) or not isinstance(c.get("evidence"), str) or not isinstance(c.get("passed"), bool):
            continue
        tail = c["evidence"].strip()[-60:]
        if m := EXPLICIT_VERDICT.search(tail):
            verdict_pass = m.group(1) == "PASS"
            if verdict_pass != c["passed"]:
                out.append({"id": c.get("id"), "passed": c["passed"], "evidence_tail": tail})
            continue
        says_pass, says_fail = bool(SAYS_PASS_ZH.search(tail)), bool(SAYS_FAIL_ZH.search(tail))
        if says_pass != says_fail and says_pass != c["passed"]:
            out.append({"id": c.get("id"), "passed": c["passed"], "evidence_tail": tail})
    return out


def recheck_prompt(original: str, reply: str, bad: list[dict]) -> str:
    feedback = json.dumps({"previous_reply": reply, "inconsistent": bad}, ensure_ascii=False, indent=2)
    return (
        "In your previous evaluation response, the items listed in `inconsistent` have a contradiction between "
        "the concluding statement in `evidence` and the boolean `passed` field.\n"
        "For every item, the conclusion stated in `evidence` and `passed` must agree. Re-verify the flagged items "
        "against the primary submission evidence under the original rubric (do not relax or tighten the rubric, and "
        "keep all other items unchanged). Return a complete JSON evaluation covering all items.\n\n"
        f"Inconsistency details:\n{feedback}\n\nOriginal evaluation request:\n{original}"
    )


class CheckedReview:
    """Wrap PiAgent reviewer to re-check evidence/boolean contradictions and retry non-truncation process errors."""

    def __init__(self, agent, rechecks: int = 1, retries: int = 1):
        self.agent, self.rechecks, self.retries = agent, rechecks, retries

    @property
    def submit_tool_name(self) -> str | None:
        return getattr(self.agent, "submit_tool_name", None)

    def ask(self, call):
        for n in range(1, self.retries + 2):
            try:
                return self.agent.run(call)
            except IncompleteResponseError:
                raise
            except ExternalExecutionError as exc:
                if n > self.retries:
                    raise
                keep_failed(Path(call.log_dir), exc)

    def run(self, call):
        reply = self.ask(call)
        for n in range(1, self.rechecks + 1):
            bad = contradictions(reply.text)
            if not bad:
                return reply
            keep_recheck(Path(call.log_dir), n, reply.text, bad)
            try:
                reply = self.ask(replace(call, prompt=recheck_prompt(call.prompt, reply.text, bad)))
            except ExternalExecutionError as exc:
                (Path(call.log_dir) / f"recheck-{n}" / "failed.txt").write_text(
                    f"Recheck failed: {type(exc).__name__}: {exc}\n", encoding="utf-8"
                )
                raise
        if bad := contradictions(reply.text):
            raise AdapterError(
                f"Reviewer returned contradictory verdict/evidence after {self.rechecks} recheck(s): {bad}"
            )
        return reply

    def close(self):
        close = getattr(self.agent, "close", None)
        if close:
            close()


def keep_recheck(log_dir: Path, n: int, text: str, bad: list[dict]) -> None:
    dst = log_dir / f"recheck-{n}"
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("stdout.log", "stderr.log"):
        if (log_dir / name).exists():
            (log_dir / name).replace(dst / name)
    (dst / "reply.txt").write_text(text, encoding="utf-8")
    (dst / "inconsistent.json").write_text(json.dumps(bad, ensure_ascii=False, indent=2), encoding="utf-8")


def keep_failed(log_dir: Path, exc: Exception) -> None:
    n = 1
    while (log_dir / f"retry-{n}").exists():
        n += 1
    dst = log_dir / f"retry-{n}"
    dst.mkdir(parents=True)
    for name in ("stdout.log", "stderr.log", "cleanup.json"):
        if (log_dir / name).exists():
            (log_dir / name).replace(dst / name)
    (dst / "error.txt").write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")


def make_review(project, rechecks: int = 1, retries: int = 1, **pi):
    from multi_round.agents.pi import PiAgent

    return CheckedReview(PiAgent(project, **pi), rechecks=rechecks, retries=retries)
