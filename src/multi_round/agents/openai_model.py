"""Stateless OpenAI-compatible chat model adapter for tool-less calls; HTTP/SSE is handled by the OpenAI SDK."""

import asyncio
import base64
import json
import mimetypes
import os
from pathlib import Path

import httpx
from openai import (
    APIConnectionError,
    APIError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
)

from ..enums import Role
from ..models import (
    AdapterError,
    Attachment,
    Call,
    ExternalExecutionError,
    IncompleteResponseError,
    OutputProtocolError,
    Reply,
    parse_attachments,
)
from ..storage import write_json
from .role_context import role_system


class OpenAICompatibleModel:
    def __init__(
        self,
        base_url: str,
        model: str,
        key_env: str = "OPENAI_API_KEY",
        key_file: str | None = None,
        timeout: float = 120,
        read_timeout: float | None = None,
        stream: bool = True,
        include_usage: bool = True,
        max_tokens: int = 32768,
        max_tokens_field: str = "max_tokens",
        temperature: float | None = 0,
        max_retries: int = 2,
    ):
        # Default read_timeout to total timeout so a shorter hidden default cannot abort slow reasoning streams early.
        if read_timeout is None:
            read_timeout = timeout
        if min(timeout, read_timeout, max_tokens) <= 0 or max_retries < 0:
            raise ValueError(
                "Timeouts and max_tokens must be positive, and max_retries must be non-negative"
            )
        if max_tokens_field not in {"max_tokens", "max_completion_tokens"}:
            raise ValueError(
                "max_tokens_field must be 'max_tokens' or 'max_completion_tokens'"
            )
        self.base_url, self.model = base_url, model
        self.key_env, self.key_file = key_env, key_file
        self.timeout, self.read_timeout = timeout, read_timeout
        self.stream, self.include_usage = stream, include_usage
        self.max_tokens, self.max_tokens_field = max_tokens, max_tokens_field
        self.temperature = temperature
        # Retries apply only while establishing the response; mid-stream disconnects preserve partial logs and fail explicitly.
        self.max_retries = max_retries

    def run(self, call: Call) -> Reply:
        if call.role == Role.WORKER:
            raise AdapterError(
                "OpenAICompatibleModel has no tools and cannot run as a Worker harness"
            )
        if call.workspace is not None:
            raise AdapterError(
                "OpenAICompatibleModel cannot inspect workspaces or file evidence; use a tool-capable harness such as pi"
            )
        key = os.environ.get(self.key_env, "")
        if not key and self.key_file:
            key = Path(self.key_file).expanduser().read_text(encoding="utf-8").strip()
        if not key:
            raise AdapterError(f"Set {self.key_env} or configure key_file")

        prompt = call.prompt
        if call.schema:
            # OpenAI-compatible relays may not support native json_schema; append Schema text and validate via shared JSON recovery.
            prompt += "\nOutput ONLY JSON conforming to the following Schema:\n" + json.dumps(
                call.schema, ensure_ascii=False
            )
        messages = [
            {
                "role": "system",
                "content": role_system(
                    call.role, has_tools=False, structured=bool(call.schema)
                )
                + "\n"
                + call.instructions,
            }
        ]
        messages += [
            {
                "role": m["role"],
                "content": message_content(
                    m["content"], parse_attachments(m.get("attachments", []))
                ),
            }
            for m in call.history
        ]
        messages.append(
            {"role": "user", "content": message_content(prompt, call.attachments)}
        )
        request = {
            "model": self.model,
            "messages": messages,
            self.max_tokens_field: self.max_tokens,
            "stream": self.stream,
        }
        if self.temperature is not None:
            request["temperature"] = self.temperature
        if self.stream and self.include_usage:
            request["stream_options"] = {"include_usage": True}
        call.log_dir.mkdir(parents=True, exist_ok=True)
        write_json(call.log_dir / "model_request.json", request)
        try:
            # Keep the outer interface synchronous while using AsyncOpenAI so total timeout cancels active streams.
            return asyncio.run(self._request(request, key, call.log_dir))
        except APITimeoutError:
            raise ExternalExecutionError(
                "Model connection or read timed out"
            ) from None
        except TimeoutError:
            raise ExternalExecutionError(
                f"Model call exceeded total timeout of {self.timeout}s"
            ) from None
        except APIStatusError as exc:
            # Omit raw response body and auth headers from exception text to avoid leaking credentials.
            raise ExternalExecutionError(
                f"Model service HTTP {exc.status_code}; request_id={exc.request_id}"
            ) from None
        except APIConnectionError:
            raise ExternalExecutionError(
                "Model service connection failed or stream was interrupted; partial stream chunks are saved in logs"
            ) from None
        except httpx.TimeoutException:
            raise ExternalExecutionError(
                "Model stream read timed out; partial stream chunks are saved in logs"
            ) from None
        except httpx.HTTPError:
            raise ExternalExecutionError(
                "Model response stream was interrupted; partial stream chunks are saved in logs"
            ) from None
        except (APIError, json.JSONDecodeError):
            raise ExternalExecutionError(
                "Model service returned an invalid response; see call logs"
            ) from None

    async def _request(self, request: dict, key: str, log_dir: Path) -> Reply:
        async with asyncio.timeout(self.timeout):
            async with AsyncOpenAI(
                base_url=self.base_url,
                api_key=key,
                max_retries=self.max_retries,
                timeout=httpx.Timeout(
                    self.timeout, read=min(self.read_timeout, self.timeout)
                ),
            ) as client:
                response = await client.chat.completions.create(**request)
                if self.stream:
                    return await self._read_stream(response, log_dir)
                write_json(log_dir / "response.json", response.model_dump(mode="json"))
                if len(response.choices) != 1:
                    raise AdapterError("Model response must contain exactly one choice")
                choice = response.choices[0]
                return completed_reply(
                    choice.message.content,
                    choice.finish_reason,
                    choice.message.refusal,
                    choice.message.tool_calls or choice.message.function_call,
                )

    async def _read_stream(self, response, log_dir: Path) -> Reply:
        parts = []
        finish_reason, usage = None, None
        # Accumulate public assistant content only; provider-specific reasoning deltas stay untouched in events.jsonl.
        async with response:
            with (log_dir / "events.jsonl").open("w", encoding="utf-8") as events:
                async for chunk in response:
                    events.write(chunk.model_dump_json() + "\n")
                    events.flush()
                    if chunk.usage is not None:
                        usage = chunk.usage.model_dump(mode="json")
                    if not chunk.choices:  # Final usage chunk may arrive with empty choices.
                        continue
                    if len(chunk.choices) != 1 or chunk.choices[0].index != 0:
                        raise AdapterError(
                            "Model stream must contain exactly one choice"
                        )
                    choice = chunk.choices[0]
                    if (
                        choice.delta.refusal
                        or choice.delta.tool_calls
                        or choice.delta.function_call
                    ):
                        raise AdapterError(
                            "Tool-less model returned a refusal or tool call"
                        )
                    if choice.delta.content:
                        parts.append(choice.delta.content)
                    if choice.finish_reason is not None:
                        finish_reason = choice.finish_reason
        text = "".join(parts)
        write_json(
            log_dir / "response.json",
            {
                "content": text,
                "finish_reason": finish_reason,
                "usage": usage,
            },
        )
        return completed_reply(text, finish_reason)


def completed_reply(
    text: str | None, finish_reason: str | None, refusal=None, tool_calls=None
) -> Reply:
    """Partial text is not a valid reply unless finish_reason=='stop'."""
    if finish_reason == "length":
        raise IncompleteResponseError("Model response truncated at max_tokens limit")
    if refusal or tool_calls or finish_reason != "stop":
        raise AdapterError(
            f"Model did not finish normally: finish_reason={finish_reason}"
        )
    if not isinstance(text, str) or not text.strip():
        raise OutputProtocolError("Model returned an empty reply")
    return Reply(text)


def message_content(text: str, attachments: list[Attachment]):
    """Inline explicit attachments into message content without granting workspace or tool access."""
    if not attachments:
        return text
    parts = [{"type": "text", "text": text}]
    for attachment in attachments:
        path = Path(attachment.path)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data_url = f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode(
            "ascii"
        )
        if attachment.kind == "image":
            if mime not in {"image/png", "image/jpeg", "image/webp", "image/gif"}:
                raise AdapterError(f"Unsupported image format: {path.name}")
            parts += [
                {"type": "text", "text": f"Image attachment: {path.name}"},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]
        elif mime == "application/pdf":
            parts.append(
                {"type": "file", "file": {"filename": path.name, "file_data": data_url}}
            )
        elif path.suffix.lower() in {".txt", ".csv", ".json", ".md"}:
            parts.append(
                {
                    "type": "text",
                    "text": f"File attachment {path.name}:\n{path.read_text(encoding='utf-8')}",
                }
            )
        else:
            raise AdapterError(
                f"OpenAICompatibleModel does not support attachment {path.name}; use a harness capable of reading it"
            )
    return parts
