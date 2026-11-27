"""Codex CLI adapter: Worker resumes its native session while User/Reviewer run as isolated ephemeral calls."""

import json
import os
import re
import tomllib
from dataclasses import asdict
from pathlib import Path

from ..enums import Role
from ..models import AdapterError, Call, Reply
from ..processes import run_process
from ..storage import write_json
from .role_context import role_system


class CodexAgent:
    """Worker resumes its native thread; User and Reviewer run isolated per call without sharing hidden state."""

    def __init__(
        self,
        executable: str = "codex",
        model: str | None = None,
        timeout: int = 240,
        provider: str | None = None,
        api_retries: int | None = None,
    ):
        self.executable, self.model, self.timeout = executable, model, timeout
        if provider is not None and (
            not isinstance(provider, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", provider)
        ):
            raise ValueError("provider must be a valid native provider identifier")
        if api_retries is not None and (
            type(api_retries) is not int or api_retries < 0 or not provider
        ):
            raise ValueError(
                "Configuring api_retries requires an explicit provider and a non-negative integer"
            )
        self.provider = provider
        self.api_retries = (
            (2 if api_retries is None else api_retries) if provider else None
        )
        self._session_instructions = {}

    def run(self, call: Call) -> Reply:
        if call.role != Role.WORKER and call.session_id:
            raise AdapterError(
                "User and Reviewer calls cannot inherit an existing session"
            )
        if call.session_id and call.session_id not in self._session_instructions:
            raise AdapterError(
                "No instruction binding recorded for this session_id; cannot resume unknown session"
            )
        if (
            call.session_id in self._session_instructions
            and self._session_instructions[call.session_id] != call.instructions
        ):
            raise AdapterError(
                "Initial instructions changed for an active Codex session; pass new task requirements via turn messages"
            )
        if call.workspace is None:
            raise AdapterError(
                "CodexAgent does not support workspace=None calls; configure progress_reviewer "
                "with pi, openai_compatible, or a text-only command wrapper"
            )
        call.log_dir.mkdir(parents=True, exist_ok=True)
        if call.role == Role.WORKER and call.history and not call.session_id:
            raise AdapterError(
                "Follow-up Codex Worker turn is missing native session_id; refusing to reconstruct session from public history"
            )
        # Native Codex threads retain full tool history; resumed turns send only the new prompt.
        prompt = call.prompt
        if call.attachments:
            # Non-image files are read from the workspace via tools; images are also passed via native --image.
            prompt += "\n\nAttachments for this message:\n" + json.dumps(
                [asdict(a) for a in call.attachments], ensure_ascii=False
            )
        (call.log_dir / "stdin.txt").write_text(prompt, encoding="utf-8")
        output = call.log_dir / "last_message.txt"
        args = [self.executable, "exec"]
        if call.session_id:
            args += ["resume", call.session_id]
        else:
            args += [
                "--sandbox",
                "workspace-write" if call.role == Role.WORKER else "read-only",
            ]
            if call.role != Role.WORKER:
                args += ["--ephemeral"]
        args += [
            "--skip-git-repo-check",
            "--json",
            "-o",
            str(output),
            "-c",
            'approval_policy="never"',
            "-c",
            "features.multi_agent=false",
            "-c",
            "features.goals=false",
            "-c",
            "features.unbounded_connection_retries=false",
            "-c",
            "skills.bundled.enabled=false",
            "-c",
            "skills.include_instructions=false",
            "-c",
            'web_search="disabled"',
            "-c",
            "features.hooks=false",
            "-c",
            "features.memories=false",
            "-c",
            "features.apps=false",
            "-c",
            "features.remote_plugin=false",
        ]
        provider, retries = self.provider, self.api_retries
        if call.role != Role.WORKER:
            # Empty table overrides still deep-merge global MCP entries; isolated roles copy only model connection settings.
            # --ignore-user-config leaves CODEX_HOME intact so native CLI auth credentials still work.
            settings = self._isolated_model_settings()
            provider = settings["model_provider"]
            retries = 2 if self.api_retries is None else self.api_retries
            args += ["--ignore-user-config", "--ignore-rules"]
            for key, value in settings.items():
                args += ["-c", key + "=" + json.dumps(value, ensure_ascii=False)]
        if provider:
            args += [
                "-c",
                "model_provider=" + json.dumps(provider),
                "-c",
                f"model_providers.{provider}.request_max_retries=0",
                "-c",
                f"model_providers.{provider}.stream_max_retries={retries}",
                "-c",
                f"model_providers.{provider}.stream_idle_timeout_ms={int(self.timeout * 1000)}",
                "-c",
                f"model_providers.{provider}.supports_websockets=false",
            ]
        write_json(
            call.log_dir / "native_settings.json",
            {
                "provider": provider,
                "retry_owner": "stream" if provider else "native_user_configuration",
                "api_retries": retries,
                "hard_timeout_seconds": self.timeout,
                "isolated_config": call.role != Role.WORKER,
            },
        )
        if self.model:
            args += ["--model", self.model]
        if call.role == Role.WORKER:
            # Native resume retains the initial developer_instructions; the binding check above prevents silent drift.
            args += [
                "-c",
                "developer_instructions="
                + json.dumps(call.instructions, ensure_ascii=False),
            ]
        else:
            system = call.log_dir / "system.md"
            system.write_text(
                role_system(call.role, has_tools=True, structured=bool(call.schema))
                + "\n"
                + call.instructions,
                encoding="utf-8",
            )
            args += [
                "-c",
                "model_instructions_file=" + json.dumps(str(system.resolve())),
                "-c",
                'developer_instructions=""',
                "-c",
                "project_doc_max_bytes=0",
            ]
        if call.schema:
            schema_path = call.log_dir / "schema.json"
            schema_path.write_text(json.dumps(call.schema), encoding="utf-8")
            args += ["--output-schema", str(schema_path)]
        for attachment in call.attachments:
            if attachment.kind == "image":
                args += ["--image", attachment.path]
        # Authentication uses Codex's own config and environment without switching login modes here.
        events = run_process(
            args + ["-"],
            cwd=call.workspace,
            stdin=prompt,
            timeout=self.timeout,
            log_dir=call.log_dir,
        )
        session_id, completed = call.session_id, False
        for line in events.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                session_id = event["thread_id"]
            if event.get("type") == "turn.completed":
                completed = True
            if event.get("type") == "turn.failed":
                raise AdapterError(f"Codex reported turn.failed; see {call.log_dir}")
        if not completed:
            raise AdapterError(
                "Codex exited without turn.completed; refusing to treat intermediate output as final"
            )
        if call.role == Role.WORKER and not session_id:
            raise AdapterError(
                "Codex did not return a thread_id for Worker session resumption"
            )
        if not output.is_file():
            raise AdapterError("Codex did not write a final message file")
        if call.role == Role.WORKER:
            self._session_instructions[session_id] = call.instructions
        return Reply(output.read_text(encoding="utf-8"), session_id)

    def _isolated_model_settings(self):
        """Preserve model/provider connection settings while stripping personal prompts, skills, MCP servers, and hooks."""
        config_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
        path = config_home / "config.toml"
        config = (
            tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        )
        provider = self.provider or config.get("model_provider", "openai")
        if not isinstance(provider, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]+", provider
        ):
            raise AdapterError(
                "Cannot determine local Codex model_provider; configure provider explicitly"
            )
        definition = config.get("model_providers", {}).get(provider, {})
        if definition.get("http_headers"):
            raise AdapterError(
                "Isolated roles do not copy static http_headers into command args; use env_http_headers instead"
            )
        values = {"model_provider": provider}
        for key in (
            "model",
            "model_reasoning_effort",
            "model_verbosity",
            "model_context_window",
            "model_auto_compact_token_limit",
            "model_reasoning_summary",
        ):
            if key in config:
                values[key] = config[key]
        for key in (
            "name",
            "base_url",
            "env_key",
            "env_key_instructions",
            "wire_api",
            "requires_openai_auth",
        ):
            if key in definition:
                values[f"model_providers.{provider}.{key}"] = definition[key]
        for key in ("query_params", "env_http_headers"):
            for name, value in definition.get(key, {}).items():
                values[f"model_providers.{provider}.{key}.{json.dumps(name)}"] = value
        return values
