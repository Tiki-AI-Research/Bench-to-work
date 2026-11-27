"""Pi SDK adapter for isolated User and Reviewer calls; Worker continues to use pluggable harness adapters."""

import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path

from ..enums import Role
from ..models import (
    AdapterError,
    Call,
    ExternalExecutionError,
    IncompleteResponseError,
    Reply,
)
from ..processes import run_process
from .role_context import role_system

INCOMPLETE_STOP_REASONS = {
    "empty_response",
    "length",
    "pause_turn",
    "model_context_window_exceeded",
}
DELIVERY_MODES = {"auto", "tool", "response_format", "text"}
SUBMIT_TOOL_NAME = "submit_review_result"


def _incomplete_stop_reason(stderr_log: Path) -> str | None:
    """Recognize only explicit non-budget incomplete stop reasons recorded by agent.mjs."""
    if not stderr_log.is_file():
        return None
    for line in reversed(
        stderr_log.read_text(encoding="utf-8", errors="replace").splitlines()
    ):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "error":
            continue
        if event.get("exhausted") is True:
            return None
        for field in ("raw_stop_reason", "stop_reason"):
            reason = event.get(field)
            if reason == "max_tokens":
                return "length"
            if isinstance(reason, str) and reason in INCOMPLETE_STOP_REASONS:
                return reason
        return None
    return None


class PiAgent:
    """Python Call/Reply bridge; agent.mjs invokes the pi SDK directly rather than the pi CLI."""

    def __init__(
        self,
        project: Path,
        base_url: str,
        model: str,
        key_env: str = "OPENAI_API_KEY",
        key_file: str | None = None,
        node: str = "node",
        timeout: int = 180,
        max_model_turns: int = 12,
        max_tokens: int = 32768,
        context_window: int = 65536,
        supports_images: bool = False,
        thinking_format: str = "none",
        compaction: dict | None = None,
        api_retries: int = 2,
        http_401_retries: int = 0,
        thinking_level: str = "off",
        reasoning: bool | None = None,
        thinking_level_map: dict | None = None,
        compat: dict | None = None,
        structured_output: bool = True,
        structured_delivery: str = "auto",
        api: str = "openai-completions",
        auth_header: bool = False,
        sandbox_image: str | None = None,
        bash_timeout: int = 120,
    ):
        if api not in {"openai-completions", "openai-responses", "anthropic-messages"}:
            raise ValueError(
                "pi api only supports 'openai-completions', 'openai-responses', or 'anthropic-messages'"
            )
        if type(auth_header) is not bool:
            raise ValueError("pi auth_header must be a boolean")
        if sandbox_image is not None and (
            not isinstance(sandbox_image, str) or not sandbox_image.strip()
        ):
            raise ValueError("pi sandbox_image must be a non-empty Docker image string or null")
        if type(bash_timeout) is not int or not 1 <= bash_timeout <= 1800:
            raise ValueError("pi bash_timeout must be an integer between 1 and 1800 seconds")
        levels = {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
        if not isinstance(thinking_level, str) or thinking_level not in levels:
            raise ValueError(
                "thinking_level must be one of: off / minimal / low / medium / high / xhigh / max"
            )
        if thinking_format not in {"none", "deepseek"}:
            raise ValueError(
                "thinking_format only supports 'none' or 'deepseek'; configure other protocols via compat.thinkingFormat"
            )
        if thinking_format != "none" and api != "openai-completions":
            raise ValueError("thinking_format='deepseek' requires api='openai-completions'")
        if compat is not None and not isinstance(compat, dict):
            raise ValueError("pi compat must be a Pi SDK compatibility dict")
        # Map 'deepseek' thinking_format shorthand onto SDK compat; other protocols configure compat directly.
        if api == "openai-completions":
            compat = {
                "supportsDeveloperRole": False,
                "supportsStore": False,
                "supportsReasoningEffort": False,
                "maxTokensField": "max_tokens",
                **(compat or {}),
            }
            if thinking_format != "none":
                if compat.get("thinkingFormat", thinking_format) != thinking_format:
                    raise ValueError("thinking_format conflicts with compat.thinkingFormat")
                compat["thinkingFormat"] = thinking_format
        elif api == "openai-responses":
            compat = {
                "supportsStrictMode": True,
                "supportsDeveloperRole": True,
                **(compat or {}),
            }
        else:
            compat = dict(compat or {})
        if reasoning is None:
            reasoning = (
                thinking_level != "off"
                or "thinkingFormat" in compat
                or bool(compat.get("supportsReasoningEffort"))
            )
        if type(reasoning) is not bool or type(structured_output) is not bool:
            raise ValueError("reasoning and structured_output must be booleans")
        if (
            not isinstance(structured_delivery, str)
            or structured_delivery not in DELIVERY_MODES
        ):
            raise ValueError(
                "pi structured_delivery only supports auto / tool / response_format / text"
            )
        if structured_delivery == "response_format":
            if api != "openai-completions":
                raise ValueError(
                    "structured_delivery='response_format' requires api='openai-completions'"
                )
            if not structured_output:
                raise ValueError(
                    "structured_delivery='response_format' conflicts with structured_output=False"
                )
        if thinking_level != "off" and not reasoning:
            raise ValueError("Non-off thinking_level requires reasoning=True")
        if thinking_level_map is not None and (
            not isinstance(thinking_level_map, dict)
            or thinking_level_map.keys() - levels
            or any(
                v is not None and not isinstance(v, str)
                for v in thinking_level_map.values()
            )
        ):
            raise ValueError(
                "thinking_level_map keys must be valid thinking levels and values must be strings or null"
            )
        # Follow SDK compaction fields; disabled by default, with SDK defaults used for omitted token budgets.
        if compaction is not None and not isinstance(compaction, dict):
            raise ValueError("pi compaction must be a configuration dict")
        compaction = {"enabled": False, **(compaction or {})}
        if compaction.keys() - {"enabled", "reserveTokens", "keepRecentTokens"}:
            raise ValueError(
                "pi compaction only supports enabled, reserveTokens, and keepRecentTokens"
            )
        if type(compaction["enabled"]) is not bool:
            raise ValueError("pi compaction.enabled must be a boolean")
        for name in ("reserveTokens", "keepRecentTokens"):
            if name in compaction and (
                type(compaction[name]) is not int or compaction[name] < 1
            ):
                raise ValueError(f"pi compaction.{name} must be a positive integer")
        self.script = project / "harnesses/pi/agent.mjs"
        self.node, self.timeout = node, timeout
        if type(api_retries) is not int or api_retries < 0:
            raise ValueError("pi api_retries must be a non-negative integer")
        if type(http_401_retries) is not int or http_401_retries < 0:
            raise ValueError("pi http_401_retries must be a non-negative integer")
        if http_401_retries:
            raise ValueError(
                "pi http_401_retries must be 0; configure retry budget via api_retries"
            )
        self.config = {
            "api": api,
            "auth_header": auth_header,
            "base_url": base_url,
            "model": model,
            "key_env": key_env,
            "key_file": str(Path(key_file).expanduser()) if key_file else None,
            "max_model_turns": max_model_turns,
            "max_tokens": max_tokens,
            "context_window": context_window,
            "supports_images": supports_images,
            "thinking_format": thinking_format,
            "thinking_level": thinking_level,
            "reasoning": reasoning,
            "thinking_level_map": thinking_level_map,
            "compat": compat,
            "structured_output": structured_output,
            "structured_delivery": structured_delivery,
            "compaction": compaction,
            "api_retries": api_retries,
            "request_timeout_ms": timeout * 1000,
            "sandbox_image": sandbox_image.strip() if sandbox_image else None,
            "bash_timeout": bash_timeout,
        }
        if min(timeout, max_model_turns, max_tokens, context_window) < 1:
            raise ValueError("pi call budgets must be positive integers")

    def resolve_delivery(self, *, has_schema: bool) -> dict:
        """Resolve structured delivery mode once per call; system prompt, tools, request body, and logs all share it."""
        if not has_schema:
            return {"mode": "text", "tool_name": None}
        setting = self.config["structured_delivery"]
        if setting == "tool":
            return {"mode": "tool", "tool_name": SUBMIT_TOOL_NAME}
        if setting == "response_format":
            return {"mode": "response_format", "tool_name": None}
        if setting == "text":
            return {"mode": "text", "tool_name": None}
        if self.config["api"] == "anthropic-messages":
            return {"mode": "tool", "tool_name": SUBMIT_TOOL_NAME}
        if self.config["api"] == "openai-responses":
            if self.config["structured_output"]:
                return {"mode": "tool", "tool_name": SUBMIT_TOOL_NAME}
            return {"mode": "text", "tool_name": None}
        if self.config["structured_output"]:
            return {"mode": "response_format", "tool_name": None}
        return {"mode": "text", "tool_name": None}

    @property
    def submit_tool_name(self) -> str | None:
        return self.resolve_delivery(has_schema=True)["tool_name"]

    def run(self, call: Call) -> Reply:
        if call.role not in {Role.USER, Role.REVIEWER} or call.session_id:
            raise AdapterError(
                "PiAgent is only for isolated User/Reviewer calls and does not resume Worker sessions"
            )
        if not self.config["supports_images"] and any(
            a.kind == "image" for a in call.attachments
        ):
            raise AdapterError(
                "Current pi model does not declare image support; select a vision model and set supports_images=true"
            )
        delivery = self.resolve_delivery(has_schema=bool(call.schema))
        submit_tool = delivery["tool_name"]
        system = role_system(
            call.role,
            has_tools=call.workspace is not None or submit_tool is not None,
            structured=bool(call.schema),
            submit_tool=submit_tool,
        )
        payload = {
            **call.identity,
            "role": call.role,
            "system": system + "\n" + call.instructions,
            "prompt": call.prompt,
            "schema": call.schema,
            "delivery": delivery,
            "workspace": str(call.workspace) if call.workspace is not None else None,
            "attachments": [asdict(a) for a in call.attachments],
            "python": sys.executable,
            "config": self.config,
        }
        # Credentials never enter serialized request logs; Node reads them from env or key_file directly.
        with tempfile.TemporaryDirectory(prefix="multi-round-pi-") as directory:
            try:
                text = run_process(
                    [self.node, str(self.script)],
                    cwd=Path(directory),
                    stdin=json.dumps(payload, ensure_ascii=False),
                    timeout=self.timeout,
                    log_dir=call.log_dir,
                )
            except ExternalExecutionError as exc:
                reason = _incomplete_stop_reason(call.log_dir / "stderr.log")
                if reason is not None:
                    raise IncompleteResponseError(
                        f"{exc}; stop_reason={reason}"
                    ) from None
                raise
        data = json.loads(text)
        return Reply(data["text"], trace_refs=data.get("trace_refs", []))
