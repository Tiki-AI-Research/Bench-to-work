"""Real OpenAI SDK against local HTTP doubles verifying SSE, stream breaks, and timeouts."""

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from openai import AsyncOpenAI

from multi_round.agents import make_agent, openai_model
from multi_round.agents.openai_model import OpenAICompatibleModel, message_content
from multi_round.models import REVIEW_SCHEMA, AdapterError, Attachment, Call


def completion(text="Done", finish="stop", **message):
    return {
        "id": "test",
        "object": "chat.completion",
        "created": 0,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text, **message},
                "finish_reason": finish,
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }


def chunk(content=None, finish=None, **delta):
    return {
        "id": "test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "test",
        "choices": [
            {
                "index": 0,
                "delta": {"content": content, **delta},
                "finish_reason": finish,
            }
        ],
    }


class Events(httpx.AsyncByteStream):
    def __init__(self, values):
        self.values = values
        self.closed = False

    async def __aiter__(self):
        for value in self.values:
            if isinstance(value, Exception):
                raise value
            yield ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode(
                "utf-8"
            )
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


@pytest.fixture
def api(monkeypatch, tmp_path):
    """Replace HTTP transport while exercising real SDK request building, SSE decoding, and cleanup."""
    monkeypatch.setenv("TEST_MODEL_KEY", "local-secret-for-tests")
    state = {
        "requests": [],
        "options": [],
        "clients": [],
        "response": httpx.Response(200, json=completion()),
    }

    def handler(request):
        state["requests"].append(json.loads(request.content))
        assert str(request.url) == "https://example.invalid/v1/chat/completions"
        return state["response"]

    def client(**options):
        state["options"].append(options)
        transport = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        state["clients"].append(transport)
        return AsyncOpenAI(**options, http_client=transport)

    monkeypatch.setattr(openai_model, "AsyncOpenAI", client)
    state["call"] = Call("reviewer", "Current request", None, tmp_path / "logs")
    yield state
    assert all(client.is_closed for client in state["clients"])


def model(**options):
    return OpenAICompatibleModel(
        "https://example.invalid/v1", "test", key_env="TEST_MODEL_KEY", **options
    )


def stream_response(api, *values):
    events = Events(values)
    api["response"] = httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=events
    )
    return events


@pytest.mark.parametrize(
    ("options", "expected_read"),
    [
        ({}, 120),
        ({"timeout": 300}, 300),
        ({"timeout": 300, "read_timeout": 60}, 60),
        ({"timeout": 300, "read_timeout": 600}, 300),
    ],
)
def test_read_timeout_follows_total_unless_explicitly_configured(
    api, options, expected_read
):
    client = make_agent(
        {
            "type": "openai_compatible",
            "base_url": "https://example.invalid/v1",
            "model": "test",
            "key_env": "TEST_MODEL_KEY",
            "stream": False,
            **options,
        },
        api["call"].log_dir.parent,
    )
    assert client.run(api["call"]).text == "Done"
    timeout = api["options"][0]["timeout"]
    assert timeout.read == expected_read
    assert timeout.connect == options.get("timeout", 120)


def test_nonstream_passes_structured_history_schema_and_options(api):
    call = api["call"]
    call.instructions = "Role instructions"
    call.history = [
        {"role": "user", "content": "Prior prompt"},
        {"role": "assistant", "content": "Prior reply"},
    ]
    call.schema = REVIEW_SCHEMA
    result = model(stream=False, max_tokens=200, temperature=None).run(call)
    request = api["requests"][0]
    assert result.text == "Done"
    assert request["messages"][0]["role"] == "system"
    assert request["messages"][0]["content"].endswith("Role instructions")
    assert "formal stage evaluation" in request["messages"][0]["content"]
    assert request["messages"][1:3] == call.history
    assert request["messages"][-1]["content"].startswith(
        "Current request\nOutput ONLY JSON conforming"
    )
    assert request["max_tokens"] == 200
    assert not {"temperature", "tools", "workspace", "stream_options"} & request.keys()
    assert api["options"][0]["max_retries"] == 2
    saved = json.loads((call.log_dir / "response.json").read_text(encoding="utf-8"))
    assert saved["usage"]["total_tokens"] == 3
    assert "local-secret-for-tests" not in (
        call.log_dir / "model_request.json"
    ).read_text(encoding="utf-8")


def test_stream_assembles_content_preserves_reasoning_and_usage(api):
    usage = {
        **chunk(),
        "choices": [],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }
    events = stream_response(
        api,
        chunk(reasoning_content="Reasoning trace"),
        chunk("ca"),
        chunk("fé🙂"),
        chunk(finish="stop"),
        usage,
    )
    assert model().run(api["call"]).text == "café🙂"
    assert api["requests"][0]["stream_options"] == {"include_usage": True}
    saved = json.loads(
        (api["call"].log_dir / "response.json").read_text(encoding="utf-8")
    )
    assert saved["finish_reason"] == "stop" and saved["usage"]["total_tokens"] == 3
    rows = [
        json.loads(line)
        for line in (api["call"].log_dir / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows[0]["choices"][0]["delta"]["reasoning_content"] == "Reasoning trace"
    assert events.closed


def test_usage_request_can_be_disabled_without_losing_content(api):
    stream_response(api, chunk("ok", "stop"))
    assert model(include_usage=False).run(api["call"]).text == "ok"
    assert "stream_options" not in api["requests"][0]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("finish", ["length", "content_filter", "tool_calls", None])
def test_abnormal_finish_is_not_a_success(api, stream, finish):
    if stream:
        stream_response(api, chunk("incomplete", finish))
    else:
        api["response"] = httpx.Response(200, json=completion("incomplete", finish))
    with pytest.raises(AdapterError, match="truncated|did not finish normally"):
        model(stream=stream).run(api["call"])
    assert len(api["requests"]) == 1


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "text, extra",
    [
        ("", {}),
        (None, {"refusal": "cannot answer"}),
        (
            "Body text",
            {
                "tool_calls": [
                    {
                        "id": "t",
                        "type": "function",
                        "function": {"name": "x", "arguments": "{}"},
                    }
                ]
            },
        ),
    ],
)
def test_empty_refusal_and_tool_calls_are_not_valid_results(api, stream, text, extra):
    if stream:
        stream_response(api, chunk(text, "stop", **extra))
    else:
        api["response"] = httpx.Response(200, json=completion(text, **extra))
    with pytest.raises(AdapterError):
        model(stream=stream).run(api["call"])


@pytest.mark.parametrize(
    "error",
    [httpx.ReadError("fixture disconnect"), httpx.ReadTimeout("fixture timeout")],
)
def test_broken_stream_keeps_partial_events_and_closes_connection(api, error):
    events = stream_response(api, chunk("partial reply"), error)
    with pytest.raises(AdapterError, match="interrupted|timed out"):
        model().run(api["call"])
    assert len(api["requests"]) == 1
    assert "partial reply" in (api["call"].log_dir / "events.jsonl").read_text(
        encoding="utf-8"
    )
    assert not (api["call"].log_dir / "response.json").exists()
    assert events.closed


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_errors_do_not_leak_body_with_retry_explicitly_disabled(api, status):
    api["response"] = httpx.Response(
        status,
        json={"error": {"message": "secret-server-body"}},
        headers={"x-request-id": "test-request"},
    )
    with pytest.raises(AdapterError, match=f"HTTP {status}") as error:
        model(max_retries=0).run(api["call"])
    assert "secret-server-body" not in str(error.value)
    assert "test-request" in str(error.value)
    assert len(api["requests"]) == 1


@pytest.mark.parametrize(
    "role, workspace", [("worker", None), ("user", True), ("reviewer", True)]
)
def test_tool_roles_are_rejected_before_api_call(api, role, workspace, tmp_path):
    with pytest.raises(AdapterError, match="harness|file evidence"):
        model().run(
            Call(role, "Request", tmp_path if workspace else None, tmp_path / "other")
        )
    assert api["requests"] == []


def test_explicit_image_input_contains_bytes_but_no_tools(api, tmp_path):
    image = tmp_path / "evidence.png"
    image.write_bytes(b"test-image-bytes")
    api["call"].attachments = [Attachment(str(image), "image")]
    model(stream=False).run(api["call"])
    part = api["requests"][0]["messages"][-1]["content"][-1]
    assert (
        base64.b64decode(part["image_url"]["url"].split(",")[1]) == image.read_bytes()
    )
    assert "tools" not in api["requests"][0]


def test_file_content_and_unsupported_formats(tmp_path):
    text = tmp_path / "note.txt"
    text.write_text("Keep original note", encoding="utf-8")
    assert (
        "Keep original note" in message_content("See attachment", [Attachment(str(text))])[-1]["text"]
    )
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"%PDF-test")
    part = message_content("See attachment", [Attachment(str(pdf))])[-1]
    assert part["file"]["file_data"].startswith("data:application/pdf;base64,")
    unsupported = tmp_path / "book.xlsx"
    unsupported.write_bytes(b"xlsx-test")
    with pytest.raises(AdapterError, match="does not support attachment"):
        message_content("See attachment", [Attachment(str(unsupported))])


def test_factory_selects_model_and_existing_pi_sdk(tmp_path):
    common = {"base_url": "https://example.invalid/v1", "model": "test"}
    assert isinstance(
        make_agent({"type": "openai_compatible", **common}, tmp_path),
        OpenAICompatibleModel,
    )
    from multi_round.agents.pi import PiAgent

    assert isinstance(make_agent({"type": "pi", **common}, tmp_path), PiAgent)


@pytest.mark.parametrize("scenario", ["headers", "drip", "idle"])
def test_real_http_timeout_cancels_request_and_preserves_partial_stream(
    tmp_path, monkeypatch, scenario
):
    """Exercise real TCP sockets to verify total and idle read timeouts end-to-end."""
    monkeypatch.setenv("TEST_MODEL_KEY", "local-test")
    # Bypass host proxy variables during loopback socket tests without altering runtime proxy support.
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    cancelled = threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(self.path)
            try:
                if scenario == "headers":
                    cancelled.wait(2)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                while not cancelled.is_set():
                    self.wfile.write(
                        ("data: " + json.dumps(chunk("part")) + "\n\n").encode()
                    )
                    self.wfile.flush()
                    cancelled.wait(2 if scenario == "idle" else 0.02)
            except OSError:
                pass  # Client socket close on timeout is expected here.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    call = Call("reviewer", "Test", None, tmp_path / "logs")
    started = time.monotonic()
    try:
        client = OpenAICompatibleModel(
            f"http://127.0.0.1:{server.server_port}/v1",
            "test",
            key_env="TEST_MODEL_KEY",
            timeout=0.6,
            read_timeout=0.15 if scenario == "idle" else 2,
        )
        with pytest.raises(AdapterError, match="timed out|total timeout"):
            client.run(call)
        assert time.monotonic() - started < 1.5
        assert requests == ["/v1/chat/completions"]
        if scenario != "headers":
            assert (call.log_dir / "events.jsonl").stat().st_size > 0
    finally:
        cancelled.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("status,expected", [(503, 3), (400, 1)])
def test_default_http_retry_policy_does_not_restart_a_scene(api, status, expected):
    api["response"] = httpx.Response(
        status, json={"error": {"message": "busy"}}, headers={"retry-after-ms": "1"}
    )
    with pytest.raises(AdapterError, match=f"HTTP {status}"):
        model(stream=False).run(api["call"])
    assert len(api["requests"]) == expected
    assert all(request == api["requests"][0] for request in api["requests"])


def test_default_output_budget_reaches_request(api):
    model(stream=False).run(api["call"])
    assert api["requests"][0]["max_tokens"] == 32768
