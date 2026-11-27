"""Minimal custom Worker example implementing the Bench-to-Work JSON stdio contract.

Reads one Call JSON from stdin, maintains persistent session state and workspace notes
inside `/workspace`, and writes one Reply JSON to stdout. Requires no external packages
or API keys so external developers can verify end-to-end Docker integration first.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    call = json.load(sys.stdin)
    if call.get("role") != "worker" or not call.get("workspace"):
        raise SystemExit("custom_worker/agent.py expects a worker Call with workspace set")

    workspace = Path(call["workspace"])
    state_file = workspace / ".custom_worker_session.json"
    note_file = workspace / "custom_worker_handoff_log.txt"

    incoming_session = call.get("session_id")
    history = call.get("history") or []

    if not history:
        if incoming_session is not None:
            raise SystemExit(f"Expected session_id=None on initial call, got {incoming_session!r}")
        session_id = f"custom-session-{call.get('run_id', 'local')}"
        state = {
            "session_id": session_id,
            "calls": [{"call_id": call["call_id"], "stage_id": call.get("stage_id"), "turn": call.get("turn")}],
        }
        state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        note_file.write_text(
            f"Turn 1 initialized by {session_id} in stage {call.get('stage_id')}.\n",
            encoding="utf-8",
        )
        reply_text = f"Initialized session {session_id} and wrote {note_file.name} in {workspace}."
    else:
        if not incoming_session:
            raise SystemExit("Follow-up call with non-empty history is missing session_id")
        if not state_file.is_file() or not note_file.is_file():
            raise SystemExit("Workspace state from prior turn was not preserved in /workspace")
        state = json.loads(state_file.read_text(encoding="utf-8"))
        if state.get("session_id") != incoming_session:
            raise SystemExit(
                f"Session mismatch: state has {state.get('session_id')!r}, call has {incoming_session!r}"
            )
        prior_note = note_file.read_text(encoding="utf-8").splitlines()[0]
        state["calls"].append(
            {"call_id": call["call_id"], "stage_id": call.get("stage_id"), "turn": call.get("turn")}
        )
        state_file.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        with note_file.open("a", encoding="utf-8") as f:
            f.write(f"Call {len(state['calls'])} resumed {incoming_session} in stage {call.get('stage_id')}.\n")
        session_id = incoming_session
        reply_text = (
            f"Resumed {session_id} on call {len(state['calls'])}; "
            f"verified prior workspace note ({prior_note!r})."
        )

    print(json.dumps({"text": reply_text, "session_id": session_id}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
