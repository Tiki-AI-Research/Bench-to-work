"""JSON-over-stdio command adapter; harness-specific session and tool handling live in the command wrapper."""

import json
import tempfile
from dataclasses import asdict
from pathlib import Path

from ..models import AdapterError, Call, Reply
from ..processes import run_process


def encode_call(call: Call) -> str:
    """Serialize public Call fields for command wrappers; controller-side log_dir is never exposed."""
    return json.dumps(
        {
            **call.identity,
            "role": call.role,
            "prompt": call.prompt,
            "workspace": str(call.workspace) if call.workspace is not None else None,
            "session_id": call.session_id,
            "schema": call.schema,
            "instructions": call.instructions,
            "history": call.history,
            "attachments": [asdict(a) for a in call.attachments],
        },
        ensure_ascii=False,
    )


def decode_reply(text: str) -> Reply:
    """Command stdout protocol is strict JSON; role-level model JSON recovery is handled by Runner."""
    data = json.loads(text)
    return Reply(
        data.get("text", ""),
        data.get("session_id"),
        trace_refs=data.get("trace_refs", []),
    )


class CommandAgent:
    """Executable JSON command protocol; CLI flags, sessions, and tool isolation are owned by the wrapper."""

    def __init__(
        self, command: list[str], timeout: int = 180, supports_text_only: bool = False
    ):
        self.command, self.timeout = command, timeout
        # Declares that the wrapper disables tools when workspace=null; this flag itself is not a sandbox.
        self.supports_text_only = supports_text_only

    def run(self, call: Call) -> Reply:
        if call.workspace is None and self.supports_text_only is not True:
            raise AdapterError(
                "CommandAgent does not declare text-only support; disable tools in the wrapper and set supports_text_only=true"
            )
        payload = encode_call(call)
        # Pass structured fields as-is; wrappers may resume native sessions or rebuild messages from history.
        # Text-only calls run in an empty temp cwd; actual tool disabling remains the wrapper's responsibility.
        if call.workspace is None:
            with tempfile.TemporaryDirectory(prefix="multi-round-text-") as directory:
                text = run_process(
                    self.command,
                    cwd=Path(directory),
                    stdin=payload,
                    timeout=self.timeout,
                    log_dir=call.log_dir,
                )
        else:
            text = run_process(
                self.command,
                cwd=call.workspace,
                stdin=payload,
                timeout=self.timeout,
                log_dir=call.log_dir,
            )
        return decode_reply(text)
