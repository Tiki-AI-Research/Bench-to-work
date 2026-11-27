"""Deterministic offline test double for end-to-end Runner smoke tests (no LLM calls)."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def context_of(prompt: str) -> dict:
    i = prompt.rfind('\n{\n  "phase": ')
    if i < 0:
        raise ValueError("Structured phase context not found in reviewer prompt")
    return json.loads(prompt[i + 1 :])


def main() -> int:
    call = json.load(sys.stdin)
    role = call["role"]
    if role == "user":
        reply = {"text": "(Smoke test user) Please complete the requested stage deliverables."}
    elif role == "worker":
        stage = call["stage_id"]
        reference = Path(os.environ["BTW_SMOKE_REFERENCE"]) / stage
        if reference.is_dir():
            shutil.copytree(reference, call["workspace"], dirs_exist_ok=True)
        reply = {"text": f"(Smoke test worker) Stage {stage} reference deliverables staged.", "session_id": "smoke"}
    else:
        context = context_of(call["prompt"])
        if context["phase"] == "progress":
            result = {"reason": "Smoke test: proceed to formal evaluation.", "decision": "review_submission", "continuation_id": None}
        else:
            result = {
                "reason": "Smoke test: mark rubric criteria passed to verify programmatic checks and pipeline.",
                "criteria": [
                    {"id": c["id"], "passed": True, "evidence": "Smoke test placeholder. Verdict: PASS."}
                    for c in context["stage"]["criteria"]
                ],
            }
        reply = {"text": json.dumps(result, ensure_ascii=False)}
    print(json.dumps(reply, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
