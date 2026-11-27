"""In-container Worker command using the same JSON stdio protocol as CommandAgent."""

import os
from pathlib import Path

from multi_round.agents.command import decode_reply, encode_call
from multi_round.models import Call, Reply
from multi_round.workspaces import WorkerWorkspace


class HarborWorker:
    def __init__(
        self,
        workspace: WorkerWorkspace,
        command: list[str],
        timeout: float = 300,
        env_vars: list[str] | None = None,
        key_files: dict[str, str] | None = None,
    ):
        self.workspace = workspace
        self.command = command
        self.timeout = timeout
        self.env_vars = list(dict.fromkeys(env_vars or []))
        self.key_files = dict(key_files or {})

    def run(self, call: Call) -> Reply:
        env: dict[str, str] = {}
        for name in self.env_vars:
            val = os.environ.get(name, "").strip()
            if not val and name in self.key_files:
                key_path = Path(self.key_files[name]).expanduser()
                if key_path.is_file():
                    val = key_path.read_text(encoding="utf-8").strip()
            if val:
                env[name] = val
        extra = {"env": env} if env else {}
        output = self.workspace.execute(
            self.command,
            stdin=encode_call(call),
            timeout=self.timeout,
            log_dir=call.log_dir,
            **extra,
        )
        return decode_reply(output)
