"""Deterministic command-protocol test double for offline Runner smoke demos (not a model)."""

import json
from pathlib import Path
import shutil
import sys


def main():
    request = json.load(sys.stdin)
    # Structured context block appended at the end of controller prompts.
    prompt = request["prompt"]
    context = json.loads(prompt[prompt.index("\n{\n") + 1:]) if request["role"] != "worker" else None
    if request["role"] == "user":
        # Offline double uses standalone user utterances rather than leaking hidden instructions.
        fixtures = Path(__file__).with_name("demo_user_messages.json")
        messages = json.loads(fixtures.read_text(encoding="utf-8"))
        text = messages[context["current_action"]["id"]]
    elif request["role"] == "worker":
        turn = int(request.get("session_id") or "0") + 1
        here = Path(__file__).resolve().parent
        target = Path(request["workspace"]) / "output/sales_report.xlsx"
        target.parent.mkdir(parents=True, exist_ok=True)
        # Copy the stage reference workbook to simulate an external Worker process.
        shutil.copy2(here / f"reference/s{turn}.xlsx", target)
        text = [
            "Updated NetAmount formulas and regional summary totals. Delivered: output/sales_report.xlsx.",
            "Added orders O106 and O107 and deduplicated existing order O103. Delivered: output/sales_report.xlsx.",
            "Updated O102 Quantity to 2, O103 Refund to 80, and O106 Region to South. Delivered: output/sales_report.xlsx.",
        ][turn - 1]
        print(json.dumps({"text": text, "session_id": str(turn)}, ensure_ascii=False))
        return
    elif context["phase"] == "progress":
        text = json.dumps({"reason": "Demo Worker reported delivery; advancing to formal review.",
                           "decision": "review_submission", "continuation_id": None}, ensure_ascii=False)
    else:
        text = json.dumps({"reason": "Deterministic demo stub validating orchestration only.",
                           "criteria": [{"id": c["id"], "passed": True,
                                         "evidence": "Demo Worker provided matching change summary (test double)."}
                                        for c in context["stage"]["criteria"]]}, ensure_ascii=False)
    print(json.dumps({"text": text}, ensure_ascii=False))


if __name__ == "__main__":
    main()
