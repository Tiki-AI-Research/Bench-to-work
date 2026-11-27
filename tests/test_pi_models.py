"""Verify Pi SDK model parameter forwarding and structured delivery modes against local HTTP servers."""

import json
from pathlib import Path

import pytest
from multi_round.agents import make_agent
from multi_round.json_protocol import parse_and_validate_json
from multi_round.models import (
    PROGRESS_SCHEMA,
    REVIEW_SCHEMA,
    Call,
    ExternalExecutionError,
    IncompleteResponseError,
)
from test_pi import PROJECT
from test_pi import model_server as model_server  # noqa: PLC0414 - pytest fixture re-export


@pytest.mark.parametrize("role", ["user", "reviewer"])
@pytest.mark.parametrize(
    "options,expected,absent",
    [
        (
            {"thinking_format": "deepseek", "thinking_level": "high"},
            {"thinking": {"type": "enabled"}},
            ["reasoning_effort"],
        ),
        (
            {"thinking_format": "deepseek", "thinking_level": "off"},
            {"thinking": {"type": "disabled"}},
            ["reasoning_effort"],
        ),
        (
            {
                "thinking_level": "high",
                "compat": {
                    "supportsReasoningEffort": True,
                    "maxTokensField": "max_completion_tokens",
                },
            },
            {"reasoning_effort": "high", "max_completion_tokens": 32768},
            ["thinking", "max_tokens"],
        ),
        (
            {
                "reasoning": True,
                "thinking_level_map": {"off": "none"},
                "compat": {"supportsReasoningEffort": True},
            },
            {"reasoning_effort": "none"},
            ["thinking"],
        ),
        (
            {
                "thinking_level": "high",
                "thinking_level_map": {"high": "medium"},
                "compat": {"supportsReasoningEffort": True},
            },
            {"reasoning_effort": "medium"},
            ["thinking"],
        ),
        (
            {"thinking_level": "high", "compat": {"thinkingFormat": "qwen"}},
            {"enable_thinking": True},
            ["thinking", "reasoning_effort"],
        ),
    ],
)
def test_pi_model_options_reach_each_role(
    model_server, monkeypatch, tmp_path, role, options, expected, absent
):
    monkeypatch.setenv("PI_TEST_KEY", "local-test-key")
    agent = make_agent(
        {
            "type": "pi",
            "model": "fixture",
            "base_url": model_server[0],
            "key_env": "PI_TEST_KEY",
            "timeout": 30,
            **options,
        },
        PROJECT,
    )
    schema = REVIEW_SCHEMA if role == "reviewer" else None
    agent.run(Call(role, "Return result", None, tmp_path / "logs", schema=schema))
    request = model_server[1][0]
    for key, value in expected.items():
        assert request[key] == value
    assert all(key not in request for key in absent)
    events = [
        json.loads(line)
        for line in (tmp_path / "logs/stderr.log").read_text().splitlines()
    ]
    settings = next(e for e in events if e["type"] == "session_config")
    assert settings["thinking_level"] == options.get("thinking_level", "off")
    assert settings["max_tokens"] == 32768
    assert settings["context_window"] == agent.config["context_window"]
    actual = next(e for e in events if e["type"] == "model_request")
    for key in ("max_tokens", "max_completion_tokens", "reasoning_effort"):
        assert actual[key] == request.get(key)
    assert "local-test-key" not in json.dumps(events)


def test_pi_logs_context_clamped_budget_instead_of_configured_limit(
    model_server, monkeypatch, tmp_path
):
    monkeypatch.setenv("PI_TEST_KEY", "local-test-key")
    agent = make_agent(
        {
            "type": "pi",
            "model": "fixture",
            "base_url": model_server[0],
            "key_env": "PI_TEST_KEY",
            "timeout": 30,
            "context_window": 8192,
            "max_tokens": 4096,
        },
        PROJECT,
    )
    # Verify that the clamped max_tokens budget sent by the SDK matches the logged model_request event.
    agent.run(
        Call(
            "reviewer",
            "Long evidence block. " * 2500,
            None,
            tmp_path / "logs",
            schema=REVIEW_SCHEMA,
        )
    )
    request = model_server[1][0]
    events = [
        json.loads(line)
        for line in (tmp_path / "logs/stderr.log").read_text().splitlines()
    ]
    settings = next(e for e in events if e["type"] == "session_config")
    actual = next(e for e in events if e["type"] == "model_request")
    assert settings["max_tokens"] == 4096
    assert actual["max_tokens"] == request["max_tokens"] == 1
    assert actual["max_completion_tokens"] is None
    assert "local-test-key" not in json.dumps(events)


@pytest.mark.parametrize("role", ["user", "reviewer"])
def test_pi_gateway_can_disable_schema_request_but_keep_output_validation(
    model_server, monkeypatch, tmp_path, role
):
    monkeypatch.setenv("PI_TEST_KEY", "local-test-key")
    agent = make_agent(
        {
            "type": "pi",
            "model": "fixture",
            "base_url": model_server[0],
            "key_env": "PI_TEST_KEY",
            "structured_output": False,
        },
        PROJECT,
    )
    model_server[2][0] = lambda _: '{"reason":"Verified","criteria":[]}'
    reply = agent.run(
        Call(
            role,
            "Return result",
            None,
            tmp_path / "logs",
            schema=REVIEW_SCHEMA if role == "reviewer" else None,
        )
    )
    request = model_server[1][0]
    assert "response_format" not in request
    if role == "reviewer":
        assert "Output must conform to the following JSON Schema" in json.dumps(
            request["messages"][-1]["content"], ensure_ascii=False
        )
        assert parse_and_validate_json(reply.text, REVIEW_SCHEMA)["criteria"] == []


@pytest.mark.parametrize(
    "options",
    [
        {"http_401_retries": -1},
        {"http_401_retries": True},
        {"http_401_retries": 1.5},
        {"thinking_level": "unknown"},
        {"thinking_level": "high", "reasoning": False},
        {"reasoning": "true"},
        {"structured_output": "false"},
        {"thinking_level_map": {"high": 123}},
        {"thinking_level_map": {"unknown": "high"}},
        {"compat": []},
        {"thinking_format": "deepseek", "compat": {"thinkingFormat": "openai"}},
    ],
)
def test_pi_rejects_inconsistent_model_configuration(options):
    with pytest.raises(ValueError):
        make_agent(
            {"type": "pi", "model": "fixture", "base_url": "http://unused", **options},
            PROJECT,
        )


def test_nonzero_http_401_retries_is_rejected_in_favor_of_api_retries():
    from multi_round.agents.pi import PiAgent
    with pytest.raises(ValueError, match="api_retries"):
        PiAgent(Path("."), base_url="https://example.test/v1", model="test", http_401_retries=2)


def test_anthropic_pi_tools_retry_and_schema(tmp_path, monkeypatch):
    """Anthropic Messages protocol via Pi SDK recovers from mid-stream truncation without re-running tools."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    requests = []

    def event(kind, **fields):
        return f"event: {kind}\ndata: {json.dumps({'type': kind, **fields})}\n\n"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            assert self.path.split('?', 1)[0] == '/v1/messages'
            assert self.headers['Authorization'] == 'Bearer local-key'
            assert 'response_format' not in body
            tool = len(requests) == 1
            content = ({'type': 'tool_use', 'id': 'read-once', 'name': 'read', 'input': {}}
                       if tool else {'type': 'text', 'text': ''})
            delta = ({'type': 'input_json_delta', 'partial_json': '{"path":"answer.json"}'}
                     if tool else {'type': 'text_delta', 'text': '{"reason":"amount=47","criteria":[]}'})
            text = event('message_start', message={'id': 'fixture', 'type': 'message',
                'role': 'assistant', 'model': 'fixture', 'content': [], 'stop_reason': None,
                'usage': {'input_tokens': 10, 'output_tokens': 0}})
            text += event('content_block_start', index=0, content_block=content)
            text += event('content_block_delta', index=0, delta=delta)
            text += event('content_block_stop', index=0)
            if len(requests) != 2:  # Truncate the first response after the tool result.
                text += event('message_delta', delta={'stop_reason': 'tool_use' if tool else 'end_turn'},
                              usage={'output_tokens': 10}) + event('message_stop')
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            self.wfile.write(text.encode())

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv('ANTHROPIC_TEST_KEY', 'local-key')
    work = tmp_path / 'work'
    work.mkdir()
    (work / 'answer.json').write_text('{"amount":47}')
    agent = make_agent({'type': 'pi', 'model': 'fixture', 'api': 'anthropic-messages',
        'auth_header': True, 'base_url': f'http://127.0.0.1:{server.server_port}',
        'key_env': 'ANTHROPIC_TEST_KEY', 'timeout': 30, 'api_retries': 1}, PROJECT)
    try:
        reply = agent.run(Call('reviewer', 'Read answer.json and return the amount.', work,
                              tmp_path / 'logs', schema=REVIEW_SCHEMA, instructions='REVIEW_ROLE_TAG'))
        assert json.loads(reply.text)['reason'] == 'amount=47'
        assert len(requests) == 3 and requests[1] == requests[2]
        assert 'REVIEW_ROLE_TAG' in json.dumps(requests[0]['system'])
        assert '47' in json.dumps(requests[-1]['messages'])
        events = [json.loads(line) for line in (tmp_path / 'logs/stderr.log').read_text().splitlines()]
        assert sum(e['type'] == 'tool_execution_start' for e in events) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("invalid_first", [False, True])
def test_anthropic_pi_submit_review_result_validates_schema_and_recovers(tmp_path, monkeypatch, invalid_first):
    """Anthropic protocol registers submit_review_result with in-session schema validation even without a workspace."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    requests = []

    def sse(kind, **fields):
        return f"event: {kind}\ndata: {json.dumps({'type': kind, **fields})}\n\n"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            turn = len(requests)
            if turn == 1 and invalid_first:
                tool_args = json.dumps({"reason": "", "criteria": "not-a-list"})
                blocks = [
                    (
                        {"type": "text", "text": 'Draft analysis {"criteria": []}'},
                        {"type": "text_delta", "text": 'Draft analysis {"criteria": []}'},
                    ),
                    (
                        {"type": "tool_use", "id": "submit-1", "name": "submit_review_result", "input": {}},
                        {"type": "input_json_delta", "partial_json": tool_args},
                    ),
                ]
            else:
                tool_args = json.dumps(
                    {
                        "reason": "Corrected arguments",
                        "criteria": [{"id": "c1", "passed": True, "evidence": "ok"}],
                    },
                    ensure_ascii=False,
                )
                blocks = [
                    (
                        {"type": "tool_use", "id": "submit-2", "name": "submit_review_result", "input": {}},
                        {"type": "input_json_delta", "partial_json": tool_args},
                    )
                ]
            text = sse(
                "message_start",
                message={
                    "id": f"m-{turn}",
                    "type": "message",
                    "role": "assistant",
                    "model": "fixture",
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            )
            for idx, (start_block, delta) in enumerate(blocks):
                text += sse("content_block_start", index=idx, content_block=start_block)
                text += sse("content_block_delta", index=idx, delta=delta)
                text += sse("content_block_stop", index=idx)
            text += sse(
                "message_delta",
                delta={"stop_reason": "tool_use"},
                usage={"output_tokens": 10},
            ) + sse("message_stop")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(text.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "local-key")
    agent = make_agent(
        {
            "type": "pi",
            "model": "fixture",
            "api": "anthropic-messages",
            "auth_header": True,
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "key_env": "ANTHROPIC_TEST_KEY",
            "timeout": 30,
            "max_model_turns": 2 if invalid_first else 1,
        },
        PROJECT,
    )
    try:
        # Even without a workspace (e.g. progress_review), Anthropic schema calls expose submit_review_result.
        reply = agent.run(
            Call("reviewer", "Return formal review result", None, tmp_path / "logs", schema=REVIEW_SCHEMA)
        )
        assert len(requests) == (2 if invalid_first else 1)
        assert [t["name"] for t in requests[0]["tools"]] == ["submit_review_result"]
        assert "submit_review_result" in json.dumps(requests[0]["system"], ensure_ascii=False)
        assert json.loads(reply.text) == {
            "reason": "Corrected arguments",
            "criteria": [{"id": "c1", "passed": True, "evidence": "ok"}],
        }
        assert json.loads((tmp_path / "logs/stdout.log").read_text())["completion_method"] == "submit_review_result"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("mode", ["length", "pause_turn", "model_context_window_exceeded", "empty_response", "exhausted"])
def test_pi_distinguishes_incomplete_replies_from_exhausted_turn_budget(tmp_path, monkeypatch, mode):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    def sse(kind, **fields):
        return f"event: {kind}\ndata: {json.dumps({'type': kind, **fields})}\n\n"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            _ = self.rfile.read(int(self.headers["Content-Length"]))
            block = (
                {"type": "tool_use", "id": "t1", "name": "submit_review_result", "input": {}}
                if mode == "exhausted"
                else {"type": "text", "text": ""}
            )
            delta = (
                {"type": "input_json_delta", "partial_json": "{}"}
                if mode == "exhausted"
                else {"type": "text_delta", "text": '{"reason":"unfinished draft'}
            )
            if mode == "empty_response":
                block = {"type": "thinking", "thinking": "", "signature": ""}
                delta = {"type": "thinking_delta", "thinking": "fixture"}
            raw_stop = {"exhausted": "tool_use", "length": "max_tokens", "empty_response": "end_turn"}.get(mode, mode)
            text = (
                sse(
                    "message_start",
                    message={
                        "id": "m-1",
                        "type": "message",
                        "role": "assistant",
                        "model": "fixture",
                        "content": [],
                        "stop_reason": None,
                        "usage": {"input_tokens": 10, "output_tokens": 0},
                    },
                )
                + sse("content_block_start", index=0, content_block=block)
                + sse("content_block_delta", index=0, delta=delta)
                + sse("content_block_stop", index=0)
                + sse(
                    "message_delta",
                    delta={"stop_reason": raw_stop},
                    usage={"output_tokens": 32},
                )
                + sse("message_stop")
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(text.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ANTHROPIC_TEST_KEY", "local-key")
    agent = make_agent(
        {
            "type": "pi",
            "model": "fixture",
            "api": "anthropic-messages",
            "auth_header": True,
            "base_url": f"http://127.0.0.1:{server.server_port}",
            "key_env": "ANTHROPIC_TEST_KEY",
            "timeout": 30,
            "max_model_turns": 1,
        },
        PROJECT,
    )
    try:
        with pytest.raises(ExternalExecutionError) as caught:
            agent.run(
                Call("reviewer", "Evaluate progress", None, tmp_path / "logs", schema=PROGRESS_SCHEMA)
            )
        events = [json.loads(line) for line in (tmp_path / "logs/stderr.log").read_text().splitlines()]
        error = next(e for e in events if e.get("type") == "error")
        if mode != "exhausted":
            assert isinstance(caught.value, IncompleteResponseError)
            assert f"stop_reason={mode}" in str(caught.value)
            assert error["stop_reason"] == mode
            assert error["raw_stop_reason"] == {"length": "max_tokens", "empty_response": "end_turn"}.get(mode, mode)
        else:
            assert type(caught.value) is ExternalExecutionError
            assert not isinstance(caught.value, IncompleteResponseError)
            assert error["exhausted"] is True
            last = next(e["message"] for e in reversed(events)
                        if e.get("type") == "message_end" and e["message"].get("role") == "assistant")
            assert error["stop_reason"] == last["stopReason"]
            assert error.get("raw_stop_reason") == last.get("rawStopReason")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize("event,expected", [
    ({"raw_stop_reason": "max_tokens"}, "length"),
    ({"raw_stop_reason": "max_tokens", "stop_reason": "error"}, "length"),
    ({"raw_stop_reason": "pause_turn", "stop_reason": "stop"}, "pause_turn"),
    ({"raw_stop_reason": "model_context_window_exceeded", "stop_reason": "error"}, "model_context_window_exceeded"),
    ({"raw_stop_reason": "max_tokens", "exhausted": True}, None),
    ({"raw_stop_reason": "refusal", "stop_reason": "error"}, None),
    ({"message": "max_tokens"}, None),
])
def test_pi_incomplete_reason_from_structured_error_event(tmp_path, event, expected):
    from multi_round.agents.pi import _incomplete_stop_reason

    log = tmp_path / "stderr.log"
    log.write_text(json.dumps({"type": "error", **event}) + "\n")
    assert _incomplete_stop_reason(log) == expected


def _openai_sse(delta: dict, finish_reason: str) -> bytes:
    first = {
        "id": "fixture",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "fixture",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    final = {
        **first,
        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
    }
    return (
        f"data: {json.dumps(first, ensure_ascii=False)}\n\n"
        f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
        "data: [DONE]\n\n"
    ).encode("utf-8")


@pytest.mark.parametrize("fallback_to_prose", [False, True])
def test_openai_pi_explicit_tool_delivery_rejects_null_and_stringified_array(
    tmp_path, monkeypatch, fallback_to_prose
):
    """OpenAI explicit tool delivery omits response_format, rejects passed=null or stringified arrays, and recovers."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("evidence", encoding="utf-8")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            turn = len(requests)
            if turn == 1:
                bad = (
                    {"reason": "r", "criteria": '[{"id":"c1","evidence":"e","passed":false}]'}
                    if fallback_to_prose
                    else {"reason": "r", "criteria": [{"id": "c1", "evidence": "e", "passed": None}]}
                )
                chunk = _openai_sse(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "sub_bad",
                                "type": "function",
                                "function": {
                                    "name": "submit_review_result",
                                    "arguments": json.dumps(bad, ensure_ascii=False),
                                },
                            }
                        ],
                    },
                    "tool_calls",
                )
            elif fallback_to_prose:
                chunk = _openai_sse(
                    {
                        "role": "assistant",
                        "content": 'Tool args rejected; returning JSON in prose:\n```json\n{"reason":"r","criteria":[{"id":"c1","evidence":"e","passed":false}]}\n```',
                    },
                    "stop",
                )
            else:
                # Preceding read + valid submit + trailing ls in one batch: trailing ls must not execute.
                chunk = _openai_sse(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "read_before",
                                "type": "function",
                                "function": {"name": "read", "arguments": '{"path":"note.txt"}'},
                            },
                            {
                                "index": 1,
                                "id": "sub_ok",
                                "type": "function",
                                "function": {
                                    "name": "submit_review_result",
                                    "arguments": json.dumps(
                                        {
                                            "reason": "Verified against evidence",
                                            "criteria": [{"id": "c1", "evidence": "e", "passed": False}],
                                        },
                                        ensure_ascii=False,
                                    ),
                                },
                            },
                            {
                                "index": 2,
                                "id": "ls_after",
                                "type": "function",
                                "function": {"name": "ls", "arguments": "{}"},
                            },
                        ],
                    },
                    "tool_calls",
                )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            self.wfile.write(chunk)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OPENAI_TOOL_TEST_KEY", "local-key")
    agent = make_agent(
        {
            "type": "pi",
            "model": "fixture",
            "api": "openai-completions",
            "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            "key_env": "OPENAI_TOOL_TEST_KEY",
            "structured_output": True,
            "structured_delivery": "tool",
            "timeout": 30,
            "max_model_turns": 3,
        },
        PROJECT,
    )
    try:
        assert agent.submit_tool_name == "submit_review_result"
        reply = agent.run(
            Call("reviewer", "Please review", workspace, tmp_path / "logs", schema=REVIEW_SCHEMA)
        )
        assert len(requests) == 2
        assert "response_format" not in requests[0]
        tool_names = [t["function"]["name"] for t in requests[0]["tools"]]
        assert "submit_review_result" in tool_names and "read" in tool_names
        # Delivery tool is declared only once in system prompt, without contradicting text-JSON or trailing Schema rules.
        assert "submit_review_result" in json.dumps(requests[0]["messages"][0], ensure_ascii=False)
        assert "Return only a single valid JSON object" not in json.dumps(requests[0]["messages"][0], ensure_ascii=False)
        assert "submit_review_result" not in json.dumps(requests[0]["messages"][1], ensure_ascii=False)
        assert "Output must conform to the following JSON Schema" not in json.dumps(requests[0]["messages"][1], ensure_ascii=False)
        tool_err = next(m["content"] for m in requests[1]["messages"] if m["role"] == "tool")
        if fallback_to_prose:
            assert "expected array, got string" in tool_err
            assert "```json" in reply.text
            out = json.loads((tmp_path / "logs/stdout.log").read_text())
            assert out["completion_method"] == "assistant_stop"
        else:
            assert "expected boolean, got null" in tool_err
            assert json.loads(reply.text)["criteria"][0]["passed"] is False
            out = json.loads((tmp_path / "logs/stdout.log").read_text())
            assert out["completion_method"] == "submit_review_result"
            events = [json.loads(line) for line in (tmp_path / "logs/stderr.log").read_text().splitlines()]
            started = [e["toolName"] for e in events if e.get("type") == "tool_execution_start"]
            assert started == ["submit_review_result", "read", "submit_review_result"]
        cfg_event = next(
            json.loads(line)
            for line in (tmp_path / "logs/stderr.log").read_text().splitlines()
            if json.loads(line).get("type") == "session_config"
        )
        assert cfg_event["delivery_mode"] == "tool"
        assert cfg_event["submit_tool"] == "submit_review_result"
        assert cfg_event["response_format_enabled"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_pi_delivery_config_validation_and_text_completion_method(model_server, tmp_path, monkeypatch):
    """Default OpenAI Pi config keeps response_format and records assistant_stop; invalid structured_delivery fails."""
    monkeypatch.setenv("PI_DEFAULT_TEST_KEY", "local-test-key")
    agent = make_agent(
        {
            "type": "pi",
            "model": "fixture",
            "base_url": model_server[0],
            "key_env": "PI_DEFAULT_TEST_KEY",
            "timeout": 30,
            "max_model_turns": 4,
        },
        PROJECT,
    )
    assert agent.submit_tool_name is None
    reply = agent.run(
        Call("reviewer", "Return review JSON directly", None, tmp_path / "logs", schema=REVIEW_SCHEMA)
    )
    assert json.loads(reply.text)["reason"] == "Received"
    stdout_payload = json.loads((tmp_path / "logs/stdout.log").read_text())
    assert stdout_payload["completion_method"] == "assistant_stop"
    cfg_event = next(
        json.loads(line)
        for line in (tmp_path / "logs/stderr.log").read_text().splitlines()
        if json.loads(line).get("type") == "session_config"
    )
    assert cfg_event["delivery_mode"] == "response_format"
    assert cfg_event["submit_tool"] is None
    assert cfg_event["response_format_enabled"] is True

    with pytest.raises(ValueError, match="structured_delivery"):
        make_agent({"type": "pi", "base_url": "http://127.0.0.1/v1", "model": "x", "structured_delivery": "bad"}, PROJECT)
    with pytest.raises(ValueError, match="openai-completions"):
        make_agent(
            {
                "type": "pi",
                "api": "anthropic-messages",
                "base_url": "http://127.0.0.1",
                "model": "x",
                "structured_delivery": "response_format",
            },
            PROJECT,
        )
    with pytest.raises(ValueError, match="structured_output=False"):
        make_agent(
            {
                "type": "pi",
                "base_url": "http://127.0.0.1/v1",
                "model": "x",
                "structured_output": False,
                "structured_delivery": "response_format",
            },
            PROJECT,
        )


def test_delivery_channel_lives_in_role_system_not_upstream_correction_prompt():
    from multi_round.agents.role_context import role_system
    from multi_round.enums import Role
    from multi_round.prompts import reviewer_correction_prompt

    prompt = reviewer_correction_prompt("Original request", "Draft", "No unique JSON")
    assert "submit_review_result" not in prompt
    assert "Submit one complete structured result conforming to the original Schema" in prompt

    plain_system = role_system(Role.REVIEWER, has_tools=True, structured=True)
    assert "submit_review_result" not in plain_system
    assert "Return only a single valid JSON object" in plain_system

    tool_system = role_system(
        Role.REVIEWER,
        has_tools=True,
        structured=True,
        submit_tool="submit_review_result",
    )
    assert "submit_review_result" in tool_system
    assert "native JSON values" in tool_system
    assert "Return only a single valid JSON object" not in tool_system


def test_openai_responses_pi_tools_retry_and_schema(tmp_path, monkeypatch):
    """OpenAI Responses protocol (/v1/responses) executes multi-turn tool calls, preserves call_id|fc_id, omits temperature/response_format, and retries truncated SSE."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    requests = []
    attempts = [0]

    def sse_event(kind, payload):
        return f"event: {kind}\ndata: {json.dumps(payload)}\n\n"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path.split("?", 1)[0] == "/v1/responses"
            assert self.headers["Authorization"] == "Bearer local-responses-key"
            assert "response_format" not in body
            assert "temperature" not in body
            assert body["reasoning"]["effort"] == "high"
            assert body["max_output_tokens"] == 16384
            attempts[0] += 1
            if attempts[0] == 1:
                # Truncated stream without response.completed -> retried by retryFetch without re-running tools
                raw = sse_event("response.created", {"type": "response.created", "response": {"id": "resp_0"}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return

            requests.append(body)
            if len(requests) == 1:
                tool_names = [t["name"] for t in body["tools"]]
                assert "read" in tool_names and "submit_review_result" in tool_names
                item = {
                    "type": "function_call",
                    "id": "fc_read_01",
                    "call_id": "call_read_01",
                    "name": "read",
                    "arguments": json.dumps({"path": "note.txt"}),
                    "status": "completed",
                }
                chunks = (
                    sse_event("response.created", {"type": "response.created", "response": {"id": "resp_1"}})
                    + sse_event("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": ""}})
                    + sse_event("response.function_call_arguments.done", {"type": "response.function_call_arguments.done", "output_index": 0, "item_id": "fc_read_01", "arguments": item["arguments"]})
                    + sse_event("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item})
                    + sse_event("response.completed", {"type": "response.completed", "response": {"id": "resp_1", "status": "completed", "output": [item], "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}})
                )
            else:
                fc_items = [x for x in body["input"] if x.get("type") == "function_call"]
                out_items = [x for x in body["input"] if x.get("type") == "function_call_output"]
                assert len(fc_items) == 1 and fc_items[0]["call_id"] == "call_read_01" and fc_items[0].get("id") == "fc_read_01"
                assert len(out_items) == 1 and out_items[0]["call_id"] == "call_read_01" and "42" in out_items[0]["output"]
                sub_args = json.dumps({"reason": "Verified 42 via /v1/responses", "criteria": []})
                sub_item = {
                    "type": "function_call",
                    "id": "fc_sub_01",
                    "call_id": "call_sub_01",
                    "name": "submit_review_result",
                    "arguments": sub_args,
                    "status": "completed",
                }
                chunks = (
                    sse_event("response.created", {"type": "response.created", "response": {"id": "resp_2"}})
                    + sse_event("response.output_item.added", {"type": "response.output_item.added", "output_index": 0, "item": {**sub_item, "arguments": ""}})
                    + sse_event("response.function_call_arguments.done", {"type": "response.function_call_arguments.done", "output_index": 0, "item_id": "fc_sub_01", "arguments": sub_args})
                    + sse_event("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": sub_item})
                    + sse_event("response.completed", {"type": "response.completed", "response": {"id": "resp_2", "status": "completed", "output": [sub_item], "usage": {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28}}})
                )
            raw = chunks.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "note.txt").write_text("value=42\n", encoding="utf-8")
    monkeypatch.setenv("TEST_RESPONSES_KEY", "local-responses-key")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        agent = make_agent(
            {
                "type": "pi",
                "api": "openai-responses",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                "model": "gpt-6.2-sol",
                "thinking_level": "high",
                "key_env": "TEST_RESPONSES_KEY",
                "max_tokens": 16384,
                "api_retries": 2,
            },
            PROJECT,
        )
        reply = agent.run(Call("reviewer", "Inspect note.txt", ws, tmp_path / "logs", schema=REVIEW_SCHEMA))
        parsed = json.loads(reply.text)
        assert parsed == {"reason": "Verified 42 via /v1/responses", "criteria": []}
        assert attempts[0] == 3 and len(requests) == 2
    finally:
        server.shutdown()
        server.server_close()
