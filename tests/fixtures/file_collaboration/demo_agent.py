"""Deterministic protocol demo and failure variants (not a model or semantic judge)."""

import argparse
import json
from pathlib import Path
import shutil
import sys


def work(request, variant):
    number = int(request.get("session_id") or "0") + 1
    workspace = Path(request["workspace"])
    target = workspace / "shared/report.json"
    if number == 1:
        text = "Done." if variant == "missed_clarification" else "Please confirm the order scope: All orders or online orders only?"
    elif number == 3:
        # Attempt a real write open so the test verifies actual OS-level PermissionError rather than a flag.
        try:
            with target.open("r+", encoding="utf-8"):
                pass
        except PermissionError:
            (workspace / "lock_observed.txt").write_text("PermissionError", encoding="utf-8")
        else:
            raise RuntimeError("Current user was not blocked by read-only permissions; lock scenario invalid")
        text = "Done." if variant == "ignore_lock" else "The file is currently unwritable; please close the file and let me know so I can continue."
    else:
        data = json.loads(target.read_text(encoding="utf-8"))
        data.update(title="September Review" if number == 2 else "September Review (Confirmed)", scope="All orders",
                    accent="#ff0000" if variant == "ignore_image" else "#2463eb")
        if variant == "stale_user_edit":
            data["note"] = "Initial note"
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        preview = workspace / "output/preview.png"
        preview.parent.mkdir(exist_ok=True)
        shutil.copy2(workspace / "materials/accent.png", preview)
        text = "Updated shared/report.json, preserved the current note, set scope to All orders, and applied swatch blue. Preview saved at output/preview.png."
    return {"text": text, "session_id": str(number)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", default="happy", choices=[
        "happy", "missed_clarification", "stale_user_edit", "ignore_image", "ignore_lock"])
    args = parser.parse_args()
    request = json.load(sys.stdin)
    if request["role"] == "worker":
        reply = work(request, args.variant)
    else:
        prompt = request["prompt"]
        context = json.loads(prompt[prompt.index("\n{\n") + 1:])
        if request["role"] == "user":
            # Deterministic fixture utterances; live User adapters generate from instruction + persona + chat.
            messages = json.loads(Path(__file__).with_name("demo_user_messages.json").read_text(encoding="utf-8"))
            result = messages[context["current_action"]["id"]]
        elif context["phase"] == "progress":
            needs_reply = "please close the file" in context["conversation"][-1]["content"].lower()
            result = {"reason": "Scripted routing on deterministic demo utterances.",
                      "decision": "request_user_reply" if needs_reply else "review_submission",
                      "continuation_id": "close_file" if needs_reply else None}
        else:
            history = Path(request["workspace"]) / context["conversation"]["path"]
            conversation = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
            conversation = conversation[context["attempt"]["stage_start_message"]:]
            checks = []
            for criterion in context["stage"]["criteria"]:
                if criterion["id"] == "clarify_scope":
                    passed = "Please confirm the order scope" in conversation[1]["content"]
                elif criterion["id"] == "ask_before_retry":
                    passed = (len(conversation) >= 4 and
                              "please close the file" in conversation[1]["content"].lower() and
                              "closed the file" in conversation[2]["content"].lower())
                else:
                    passed = "shared/report.json" in conversation[-1]["content"]
                checks.append({"id": criterion["id"], "passed": passed,
                               "evidence": "Checked scripted demo utterances and turn order for regression testing."})
            result = {"reason": "Deterministic test double; not a semantic model review.", "criteria": checks}
        reply = {"text": result if request["role"] == "user" else json.dumps(result, ensure_ascii=False)}
    print(json.dumps(reply, ensure_ascii=False))


if __name__ == "__main__":
    main()
