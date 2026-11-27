"""Real Pi SDK against a local HTTP server verifying read-only tool loops and workspace isolation."""

import json
import shutil
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
from multi_round.agents import make_agent
from multi_round.agents.command import CommandAgent
from multi_round.models import (
    REVIEW_SCHEMA,
    Action,
    AdapterError,
    Call,
    Criterion,
    Reply,
    Stage,
    Task,
    UserMessage,
    load_task,
)
from multi_round.runner import Runner
from task_helpers import load_module

PROJECT = Path(__file__).resolve().parents[1]
SDK = PROJECT / "harnesses/pi/node_modules/@earendil-works/pi-coding-agent"


@pytest.fixture
def model_server():
    requests = []
    respond = [lambda payload: '{"reason":"Received","criteria":[]}' if payload.get("response_format") else "Received"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            reply = respond[0](payload)
            if isinstance(reply, int):
                self.send_response(reply)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": {"message": "invalid max_tokens=50000; incident 503" if reply == 400 else "injected transient", "type": "server_error"}}).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if isinstance(reply, tuple):
                self.wfile.write(reply[0].encode())
                return
            if reply is None:
                chunk = {"id": "cut", "object": "chat.completion.chunk", "created": 0,
                         "model": "fixture", "choices": [{"index": 0,
                         "delta": {"role": "assistant", "content": "incomplete fragment"}, "finish_reason": None}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                return
            delta = ({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_read",
                      "type": "function", "function": reply}]} if isinstance(reply, dict)
                     else {"role": "assistant", "content": reply})
            chunk = {"id": "local-test", "object": "chat.completion.chunk", "created": 0,
                     "model": "fixture", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            chunk["choices"] = [{"index": 0, "delta": {},
                                 "finish_reason": "tool_calls" if isinstance(reply, dict) else "stop"}]
            self.wfile.write(("data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n").encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests, respond
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.fixture
def pi_agent(model_server, monkeypatch):
    if not shutil.which("node") or not SDK.is_dir():
        pytest.skip("Run npm ci --prefix harnesses/pi --ignore-scripts first")
    monkeypatch.setenv("PI_TEST_KEY", "local-test-key")
    return make_agent({"type": "pi", "model": "fixture", "base_url": model_server[0],
                       "key_env": "PI_TEST_KEY", "timeout": 30}, PROJECT)


@pytest.mark.parametrize("api_retries", [0, 2])
def test_http_retry_preserves_the_role_without_restarting_session(pi_agent, model_server, tmp_path, api_retries):
    _, requests, respond = model_server
    pi_agent.config["api_retries"] = api_retries
    respond[0] = lambda _: 503 if len(requests) == 1 else "Received"
    if api_retries == 0:
        with pytest.raises(AdapterError, match="exited with code"):
            pi_agent.run(Call("user", "Current turn request", None, tmp_path / "call"))
        assert len(requests) == 1
        return
    reply = pi_agent.run(Call("user", "Current turn request", None, tmp_path / "call"))
    assert reply.text == "Received"
    assert len(requests) == 2
    assert requests[0]["messages"] == requests[1]["messages"]


def test_stream_recovery_does_not_repeat_completed_tools(pi_agent, model_server, tmp_path):
    _, requests, respond = model_server
    work = tmp_path / "work"
    work.mkdir()
    (work / "answer.json").write_text('{"amount":47}')
    after_tool = []

    def response(payload):
        if payload["messages"][-1]["role"] != "tool":
            return {"name": "read", "arguments": '{"path":"answer.json"}'}
        after_tool.append(payload)
        return None if len(after_tool) == 1 else "total=47"

    respond[0] = response
    log = tmp_path / "call"
    reply = pi_agent.run(Call("reviewer", "Read total", work, log, instructions="REVIEW_ROLE_TAG"))
    assert reply.text == "total=47" and len(requests) == 3
    assert after_tool[0]["messages"] == after_tool[1]["messages"]
    events = [json.loads(line) for line in (log / "stderr.log").read_text().splitlines()]
    assert sum(e["type"] == "tool_execution_start" for e in events) == 1
    assert any(e["type"] == "http_request_failure" for e in events)
    assert "REVIEW_ROLE_TAG" in requests[0]["messages"][0]["content"]
    assert "REVIEW_ROLE_TAG" not in requests[0]["messages"][-1]["content"]


@pytest.mark.parametrize("status", [403])
def test_permission_rejection_is_not_retried(pi_agent, model_server, tmp_path, status):
    _, requests, respond = model_server
    respond[0] = lambda _: status
    with pytest.raises(AdapterError, match="exited with code"):
        pi_agent.run(Call("user", "Current turn request", None, tmp_path / "call"))
    assert len(requests) == 1


@pytest.mark.parametrize("role", ["user", "reviewer"])
def test_pi_native_tool_reads_actual_file(pi_agent, model_server, tmp_path, role):
    workspace = tmp_path / "visible"
    workspace.mkdir()
    (workspace / "answer.json").write_text('{"amount":47}', encoding="utf-8")
    (workspace / "AGENTS.md").write_text("PROJECT_INSTRUCTION_MUST_NOT_LOAD", encoding="utf-8")
    (tmp_path / "private.txt").write_text("HIDDEN_RUBRIC", encoding="utf-8")

    def respond(payload):
        messages = payload["messages"]
        if messages[-1]["role"] != "tool":
            return {"name": "read", "arguments": '{"path":"answer.json"}'}
        amount = json.loads(messages[-1]["content"])["amount"]
        text = f"Your previous total was {amount}; please narrow to North."
        return json.dumps({"reason": text, "criteria": []}) if role == "reviewer" else text

    model_server[2][0] = respond
    log_dir = tmp_path / "logs"
    reply = pi_agent.run(Call(role, "Read answer.json and cite total", workspace, log_dir, schema=REVIEW_SCHEMA if role == "reviewer" else None))
    text = json.loads(reply.text)["reason"] if role == "reviewer" else reply.text
    assert text == "Your previous total was 47; please narrow to North."
    assert len(model_server[1]) == 2
    tools = [t["function"]["name"] for t in model_server[1][0]["tools"]]
    expected = ["ls", "read", "read_json", "read_excel", "read_docx", "read_pdf", "read_pptx"]
    assert tools == (["read_conversation", *expected] if role == "reviewer" else expected)
    requests = json.dumps(model_server[1])
    assert "PROJECT_INSTRUCTION_MUST_NOT_LOAD" not in requests and "HIDDEN_RUBRIC" not in requests
    logs = (log_dir / "stderr.log").read_text(encoding="utf-8")
    assert "tool_execution_end" in logs and "local-test-key" not in logs
    assert (workspace / "answer.json").read_text(encoding="utf-8") == '{"amount":47}'


@pytest.mark.parametrize("role", ["user", "reviewer"])
def test_pi_text_call_has_no_tools_and_no_previous_session(pi_agent, model_server, tmp_path, role):
    first = pi_agent.run(Call(role, "First text call", None, tmp_path / "a", schema=REVIEW_SCHEMA if role == "reviewer" else None))
    second = pi_agent.run(Call(role, "Second text call", None, tmp_path / "b", schema=REVIEW_SCHEMA if role == "reviewer" else None))
    assert first.session_id is second.session_id is None
    assert len(model_server[1]) == 2
    for request in model_server[1]:
        assert ("response_format" in request) == (role == "reviewer")
        final_prompt = request["messages"][-1]["content"]
        assert ("Output must conform to the following JSON Schema" in json.dumps(final_prompt, ensure_ascii=False)) == (role == "reviewer")
    assert all(not request.get("tools") for request in model_server[1])
    assert "First text call" not in json.dumps(model_server[1][1], ensure_ascii=False)
    with pytest.raises(AdapterError, match="only for isolated"):
        pi_agent.run(Call("worker", "Execute", tmp_path, tmp_path / "bad"))


@pytest.mark.parametrize(
    "raw", ['```json\n{"reason": "ok", "criteria": []}\n```', 'Confirmed:\n{"reason": "ok", "criteria": []}']
)
def test_pi_preserves_model_text_for_shared_recovery(pi_agent, model_server, tmp_path, raw):
    model_server[2][0] = lambda payload: raw
    reply = pi_agent.run(Call("reviewer", "Return review JSON", None, tmp_path / "logs", schema=REVIEW_SCHEMA))
    assert reply.text == raw
    assert len(model_server[1]) == 1  # Syntax extraction does not resend an HTTP request.


@pytest.mark.parametrize("write_only", [False, True])
def test_pi_reads_excel_formulas_without_writing(pi_agent, model_server, tmp_path, write_only):
    from openpyxl import Workbook

    read_excel = load_module(PROJECT / "harnesses/pi/read_excel.py").read_excel

    book = Workbook(write_only=write_only)
    sheet = book.create_sheet("Sheet") if write_only else book.active
    sheet.append([7])
    sheet.append(["=A1*2"])
    filename = tmp_path / "report.xlsx"
    book.save(filename)
    before = filename.read_bytes()
    assert read_excel(filename, None, 1, 50)["sheets"] == [
        {"name": "Sheet", "rows": 2, "columns": 1}
    ]

    def respond(payload):
        last = payload["messages"][-1]
        if last["role"] != "tool":
            return {"name": "read_excel", "arguments": '{"path":"report.xlsx","sheet":"Sheet"}'}
        result = json.loads(last["content"])
        assert result["rows"][0][0]["value"] == 7
        assert "cached_value" not in result["rows"][0][0]
        assert result["rows"][1][0]["value"] == "=A1*2"
        assert result["rows"][1][0]["cached_value"] is None
        return '{"reason":"Read 7 in A1 and formula in A2 with no cached value.","criteria":[]}'

    model_server[2][0] = respond
    reply = pi_agent.run(Call("reviewer", "Read Excel", tmp_path, tmp_path / "logs", schema=REVIEW_SCHEMA))
    assert "formula" in json.loads(reply.text)["reason"]
    assert filename.read_bytes() == before


def test_pi_tool_loop_has_a_model_turn_budget(pi_agent, model_server, tmp_path):
    pi_agent.config["max_model_turns"] = 2
    (tmp_path / "answer.json").write_text("{}", encoding="utf-8")
    model_server[2][0] = lambda payload: {"name": "read", "arguments": '{"path":"answer.json"}'}
    with pytest.raises(AdapterError, match="exited with code"):
        pi_agent.run(Call("user", "Repeatedly read", tmp_path, tmp_path / "logs"))
    assert len(model_server[1]) == 2
    assert "exceeded max_model_turns budget" in (tmp_path / "logs/stderr.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("broken_file", [False, True])
def test_pi_excel_errors_preserve_cause(pi_agent, model_server, tmp_path, broken_file):
    from openpyxl import Workbook

    filename = tmp_path / "coaching.xlsx"
    if broken_file:
        filename.write_bytes(b"not an xlsx file")
    else:
        book = Workbook()
        book.active.title = "Coaching_Log"
        book.active.append(["Record", 7])
        book.save(filename)
    before = filename.read_bytes()

    def respond(payload):
        last = payload["messages"][-1]
        if last["role"] != "tool":
            return {"name": "read_excel", "arguments": '{"path":"coaching.xlsx","sheet":"Coaching"}'}
        error = last["content"]
        if broken_file:
            assert "BadZipFile" in error
        else:
            assert "not found" in error
            assert "Coaching_Log" in error
        return '{"reason":"Tool reported exact underlying cause","criteria":[]}'

    model_server[2][0] = respond
    reply = pi_agent.run(Call("reviewer", "Read Excel", tmp_path, tmp_path / "logs", schema=REVIEW_SCHEMA))
    assert "exact underlying cause" in reply.text
    assert filename.read_bytes() == before


def test_pi_reads_large_verifier_evidence_from_review_file(pi_agent, model_server, tmp_path):
    from multi_round.models import Check

    marker = "OBSERVATION_ONLY_IN_FILE"
    script = tmp_path / "verifier.py"
    script.write_text(
        "import json\nprint(json.dumps({'checks':[{'id':'artifact','passed':True,'evidence':'Checked'}],"
        f"'observations':{{'actual':47,'marker':'{marker}','detail':'x'*50000}}}}))",
        encoding="utf-8",
    )
    task = Task("evidence", "Programmatic Evidence", "User", "Background", [Stage(
        "s1", "Verify", Action("start", UserMessage(type="fixed", text="Please submit result")),
        "Review upon completion", [Check("artifact", "Artifact check")],
        [Criterion("actual", "Programmatic actual value equals 47")],
    )], [sys.executable, str(script)], tmp_path)
    pages = []

    def respond(payload):
        last = payload["messages"][-1]
        if last["role"] == "tool":
            result = json.loads(last["content"])
            pages.append(result)
            if result["pointer"] == "/observations/actual":
                assert result["value"] == 47
                return {"name": "read_json", "arguments": '{"path":"evaluation/verification.json","pointer":"/observations/marker"}'}
            assert result["value"] == marker
            return '{"reason":"Read programmatic observations","criteria":[{"id":"actual","passed":true,"evidence":"evaluation/verification.json observations.actual=47"}]}'
        content = last["content"]
        text = content if isinstance(content, str) else "\n".join(p.get("text", "") for p in content)
        data, _ = json.JSONDecoder().raw_decode(text[text.index("\n{\n") + 1:])
        if data["phase"] == "progress":
            return '{"reason":"Delivered","decision":"review_submission","continuation_id":null}'
        assert marker not in text and len(text) < 10000
        assert data["verifier_results"]["path"] == "evaluation/verification.json"
        return {"name": "read_json", "arguments": '{"path":"evaluation/verification.json","pointer":"/observations/actual"}'}

    model_server[2][0] = respond
    worker = SimpleNamespace(run=lambda call: Reply("Done"))
    report = Runner(pi_agent, worker, pi_agent, progress=lambda _: None).run(task, tmp_path / "run")
    assert report["status"] == "completed"
    assert len(pages) == 2
    assert report["stages"][0]["turns"][0]["verification"]["observations"]["marker"] == marker


def test_pi_review_reads_long_dialogue_file_without_worker_submission(pi_agent, model_server, tmp_path):
    """Real Pi SDK paginates through conversation.jsonl so long earlier replies remain inspectable."""
    task = Task("dialogue", "Dialogue Review", "User", "Background", [
        Stage("s1", "Clarify", Action("start", UserMessage(type="fixed", text="Please confirm scope")),
              "Replied", [], [Criterion("history", "First reply confirmed scope boundary")],
              needs_workspace=False, max_turns=2,
              default_reply=UserMessage(type="fixed", text="Please continue"))
    ], [], tmp_path)
    early = "EARLY_BOUNDARY_EVIDENCE " + "long-turn " * 5000
    worker_replies = iter([early, "Done"])
    worker = SimpleNamespace(run=lambda call: Reply(next(worker_replies), "worker-session"))
    pages = []
    messages = {}

    def respond(payload):
        last = payload["messages"][-1]
        if last["role"] == "tool":
            result = json.loads(last["content"])
            pages.append(result)
            for message in result["messages"]:
                prior = messages.get(message["index"], {}).get("content", "")
                assert message["content_offset"] == len(prior)
                messages[message["index"]] = {**message, "content": prior + message["content"]}
            if result["has_more"]:
                return {"name": "read_conversation", "arguments": json.dumps(result["next"])}
            assert list(messages) == [0, 1, 2, 3]
            assert messages[1]["content"] == early
            assert messages[3]["content"] == "Done"
            return json.dumps({"reason": "Evaluated full history", "criteria": [
                {"id": "history", "passed": True, "evidence": "Message index=1 confirmed boundary"}
            ]})
        content = last["content"]
        text = content if isinstance(content, str) else "\n".join(p.get("text", "") for p in content)
        data, _ = json.JSONDecoder().raw_decode(text[text.index("\n{\n") + 1:])
        if data["phase"] == "progress":
            assert not payload.get("tools")
            decision = "request_user_reply" if len(data["conversation"]) == 2 else "review_submission"
            return json.dumps({"reason": "Advance by turn count", "decision": decision, "continuation_id": None})
        assert early not in text and "EARLY_BOUNDARY_EVIDENCE" not in text
        assert data["latest_reply"]["content"] == "Done"
        assert data["conversation"]["message_count"] == 4
        assert {t["function"]["name"] for t in payload["tools"]} == {
            "read", "ls", "read_json", "read_excel", "read_docx", "read_pdf", "read_pptx", "read_conversation"
        }
        return {"name": "read_conversation", "arguments": '{"start_index":0,"end_index":3}'}

    model_server[2][0] = respond
    out = tmp_path / "run"
    report = Runner(pi_agent, worker, pi_agent, progress=lambda _: None).run(task, out)
    assert report["status"] == "completed"
    assert len(pages) >= 2
    review = out / report["stages"][0]["turns"][1]["review_workspace"]
    assert not (review / "submission").exists()
    assert list((review / "scratch").iterdir()) == []
    saved = [json.loads(line) for line in (review / "evaluation/conversation.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [m["content"] for m in saved] == [m["content"] for m in messages.values()]
    assert not (out / "turns/s1-01/review").exists()  # Follow-up turns do not materialize review/ early.
    assert not list((out / "user_workspace").glob("**/conversation.jsonl"))


def test_pi_default_excel_read_handles_workbook_without_dimensions(pi_agent, model_server, tmp_path):
    from openpyxl import Workbook

    book = Workbook(write_only=True)
    sheet = book.create_sheet("results")
    sheet.append(["amount"])
    sheet.append([47])
    book.save(tmp_path / "answer.xlsx")

    def respond(payload):
        last = payload["messages"][-1]
        if last["role"] != "tool":
            return {"name": "read_excel", "arguments": '{"path":"answer.xlsx","sheet":"results"}'}
        evidence = json.loads(last["content"])
        assert evidence["rows"][1][0]["value"] == 47
        assert evidence["rows"][1][0]["cell"] == "A2"
        return json.dumps({"reason": "Read A2=47", "criteria": []})

    model_server[2][0] = respond
    reply = pi_agent.run(Call("reviewer", "Inspect workbook", tmp_path, tmp_path / "logs", schema=REVIEW_SCHEMA))
    assert json.loads(reply.text)["reason"] == "Read A2=47"
    assert len(model_server[1]) == 2


def test_pi_user_generates_next_request_from_real_previous_submission(pi_agent, model_server, tmp_path):
    task_dir = tmp_path / "task"
    shutil.copytree(PROJECT / "tests/fixtures/artifact_followup", task_dir)
    # Use a distinct sum (47.0 vs. fixture's 35.0) to prove User reads the actual delivered artifact.
    (task_dir / "materials/orders.csv").write_text("id,region,amount\nA,N,22\nB,S,20\nC,N,5\n", encoding="utf-8")
    task = load_task(task_dir / "task.json")

    def respond(payload):
        last = payload["messages"][-1]
        if last["role"] == "tool":
            amount = json.loads(last["content"])["amount"]
            return f"Your previous report total was {amount}; please narrow to region N in the same file."
        content = last["content"]
        text = content if isinstance(content, str) else "\n".join(p.get("text", "") for p in content)
        if '"id": "narrow"' in text:
            return {"name": "read", "arguments": '{"path":"output/summary.json"}'}
        return "Please summarize all orders into output/summary.json."

    model_server[2][0] = respond

    def judge(call):
        data = json.loads(call.prompt[call.prompt.index("\n{\n") + 1:])
        if data["phase"] == "progress":
            return Reply('{"reason":"Delivered","decision":"review_submission","continuation_id":null}')
        return Reply('{"reason":"Pipeline check","criteria":[{"id":"explain_delivery","passed":true,"evidence":"Delivered by stub"}]}')

    worker = CommandAgent([sys.executable, str(task_dir / "worker.py")])
    out = tmp_path / "run"
    report = Runner(pi_agent, worker, SimpleNamespace(run=judge), progress=lambda _: None).run(task, out)
    assert report["status"] == "completed"
    assert "47.0" in report["stages"][1]["turns"][0]["user"]
    assert report["stages"][0]["turns"][0]["user_visible_files"] == ["materials/orders.csv"]
    assert report["stages"][1]["turns"][0]["user_visible_files"] == ["materials/orders.csv", "output/summary.json"]
    assert json.loads((out / "user_workspace/output/summary.json").read_text(encoding="utf-8"))["amount"] == 27
    assert "tool_execution_end" in (out / "turns/s2-01/user/stderr.log").read_text(encoding="utf-8")
    assert not model_server[1][0].get("tools")  # Stage 1 uses context mode without workspace tools.
    assert model_server[1][1]["tools"]  # Stage 2 uses workspace mode to read output/summary.json.
    assert [s["turns"][0]["user_message_type"] for s in report["stages"]] == ["context", "workspace"]


def test_response_recovery_preserves_tools_and_normal_turn_budget(pi_agent, model_server, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "answer.json").write_text('{"amount":47}')
    requests = model_server[1]
    def respond(payload):
        if len(requests) == 1:
            return {"name": "read", "arguments": '{"path":"answer.json"}'}
        return None if len(requests) == 2 else "total=47"
    model_server[2][0] = respond
    pi_agent.config["max_model_turns"] = 2
    reply = pi_agent.run(Call("reviewer", "Read total", work, tmp_path / "call"))
    assert reply.text == "total=47" and len(requests) == 3
    assert requests[1]["messages"] == requests[2]["messages"]
    events = [json.loads(line) for line in (tmp_path / "call/stderr.log").read_text().splitlines()]
    assert sum(e["type"] == "tool_execution_start" for e in events) == 1
    assert any(e["type"] == "http_request_failure" for e in events)


@pytest.mark.parametrize("status", [403, 413, 422])
def test_terminal_http_status_cannot_be_overridden_by_error_body(pi_agent, model_server, tmp_path, status):
    model_server[2][0] = lambda _: status
    with pytest.raises(AdapterError):
        pi_agent.run(Call("user", "Current turn request", None, tmp_path / "call"))
    assert len(model_server[1]) == 1


def test_bad_request_is_retried_within_the_shared_budget(pi_agent, model_server, tmp_path):
    # Some gateways surface upstream transient rejections as HTTP 400; bound them by the shared retry budget.
    requests, respond = model_server[1], model_server[2]
    pi_agent.config["api_retries"] = 2
    respond[0] = lambda _: 400
    with pytest.raises(AdapterError):
        pi_agent.run(Call("user", "Current turn request", None, tmp_path / "call"))
    assert len(requests) == 3
    assert all(request == requests[0] for request in requests)


@pytest.mark.parametrize("retries", [0, 1, 2])
def test_configured_401_retry_is_bounded(pi_agent, model_server, tmp_path, retries):
    requests = model_server[1]
    pi_agent.config["api_retries"] = retries
    model_server[2][0] = lambda _: 401
    with pytest.raises(AdapterError):
        pi_agent.run(Call("user", "Current turn request", None, tmp_path / "call"))
    assert len(requests) == retries + 1
    assert all(request == requests[0] for request in requests)


def test_401_recovery_keeps_completed_tools_and_normal_turn_budget(pi_agent, model_server, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "answer.json").write_text('{"amount":47}')
    requests = model_server[1]
    pi_agent.config.update(api_retries=2, max_model_turns=2)
    def respond(payload):
        if len(requests) == 1:
            return {"name": "read", "arguments": '{"path":"answer.json"}'}
        return 401 if len(requests) <= 3 else "total=47"
    model_server[2][0] = respond
    log = tmp_path / "call"
    reply = pi_agent.run(Call("reviewer", "Read total", work, log))
    assert reply.text == "total=47" and len(requests) == 4
    assert requests[1] == requests[2] == requests[3]
    events = [json.loads(line) for line in (log / "stderr.log").read_text().splitlines()]
    assert sum(e["type"] == "tool_execution_start" for e in events) == 1
    assert sum(e["type"] == "http_request_failure" and e["retry"] for e in events) == 2
    assert not any(e["type"] == "auto_retry_start" for e in events)
    assert "local-test-key" not in json.dumps(events)


def test_http_errors_share_one_request_retry_budget(pi_agent, model_server, tmp_path):
    requests = model_server[1]
    pi_agent.config["api_retries"] = 2
    statuses = [401, 503, 401, 503, 401]
    model_server[2][0] = lambda _: statuses[len(requests) - 1]
    with pytest.raises(AdapterError):
        pi_agent.run(Call("user", "Current turn request", None, tmp_path / "call"))
    assert len(requests) == 3  # 401 and 503 share the same per-request retry budget of 2.
    assert all(request == requests[0] for request in requests)


def test_401_compatibility_does_not_retry_403(pi_agent, model_server, tmp_path):
    pi_agent.config["api_retries"] = 2
    model_server[2][0] = lambda _: 403
    with pytest.raises(AdapterError):
        pi_agent.run(Call("user", "Current turn request", None, tmp_path / "call"))
    assert len(model_server[1]) == 1


def test_done_without_finish_reason_recovers_before_sdk_and_keeps_tools(pi_agent, model_server, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "answer.json").write_text('{"amount":47}')
    requests = model_server[1]
    def respond(payload):
        if len(requests) == 1:
            return {"name": "read", "arguments": '{"path":"answer.json"}'}
        if len(requests) == 2:
            return ('data: {"choices":[{"delta":{"content":"broken partial"},"finish_reason":null}]}\n\ndata: [DONE]\n\n',)
        return "total=47"
    model_server[2][0] = respond
    pi_agent.config["max_model_turns"] = 2
    reply = pi_agent.run(Call("reviewer", "Read total", work, tmp_path / "call"))
    assert reply.text == "total=47" and len(requests) == 3
    assert requests[1] == requests[2]
    events = [json.loads(line) for line in (tmp_path / "call/stderr.log").read_text().splitlines()]
    assert sum(e["type"] == "tool_execution_start" for e in events) == 1
    assert any(e["type"] == "http_request_failure" and e["kind"] == "incomplete_stream" for e in events)
    assert not any("broken partial" in str(e.get("message", {})) for e in events)


def test_pi_reviewer_reads_docx_directly_when_stage_has_no_verifier_checks(pi_agent, model_server, tmp_path):
    """Reviewer can inspect submitted .docx files directly via read_docx even without verifier_checks."""
    import zipfile

    task = Task(
        "docx_only_stage",
        "DOCX-Only Stage Review",
        "User",
        "Background",
        [
            Stage(
                "s1",
                "Submit CMR form",
                Action("start", UserMessage(type="fixed", text="Please submit CMR.docx")),
                "Delivered",
                [],
                [Criterion("cmr_fields", "CMR includes ARC-10482 and $2,400")],
            )
        ],
        [],
        tmp_path,
    )

    def run_worker(call):
        with zipfile.ZipFile(call.workspace / "CMR.docx", "w") as zf:
            zf.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>Account ARC-10482 | Credit $2,400</w:t></w:r></w:p></w:body>"
                "</w:document>",
            )
        return Reply("Delivered CMR.docx")

    def respond(payload):
        last = payload["messages"][-1]
        if last["role"] == "tool":
            doc = json.loads(last["content"])
            assert "ARC-10482" in doc["content"] and "$2,400" in doc["content"]
            return json.dumps(
                {
                    "reason": "Verified CMR.docx body via read_docx",
                    "criteria": [{"id": "cmr_fields", "passed": True, "evidence": "submission/CMR.docx: ARC-10482, $2,400. Verdict: PASS."}],
                }
            )
        content = last["content"]
        text = content if isinstance(content, str) else "\n".join(p.get("text", "") for p in content)
        data, _ = json.JSONDecoder().raw_decode(text[text.index("\n{\n") + 1 :])
        if data["phase"] == "progress":
            return '{"reason":"Delivered","decision":"review_submission","continuation_id":null}'
        assert data["verifier_results"] is None
        return {"name": "read_docx", "arguments": '{"path":"submission/CMR.docx"}'}

    model_server[2][0] = respond
    out = tmp_path / "run"
    report = Runner(pi_agent, SimpleNamespace(run=run_worker), pi_agent, progress=lambda _: None).run(task, out)
    assert report["status"] == "completed"
    turn = report["stages"][0]["turns"][0]
    assert "verification" not in turn
    assert turn["review"]["criteria"][0]["passed"] is True


def test_pi_sandbox_bash_config_validation_and_role_isolation(model_server, tmp_path, monkeypatch):
    import os
    import subprocess

    if not shutil.which("node") or not SDK.is_dir():
        pytest.skip("Run npm ci --prefix harnesses/pi --ignore-scripts first")
    monkeypatch.setenv("PI_TEST_KEY", "local-test-key")

    for bad in ({"sandbox_image": ""}, {"sandbox_image": "   "}, {"bash_timeout": 0}, {"bash_timeout": 9999}):
        with pytest.raises(ValueError):
            make_agent(
                {"type": "pi", "model": "fixture", "base_url": model_server[0], "key_env": "PI_TEST_KEY", **bad},
                PROJECT,
            )

    image = None
    candidates = tuple(
        filter(None, (os.environ.get("MULTI_ROUND_TEST_SANDBOX_IMAGE"), "python:3.11-slim", "python:3.12-slim"))
    )
    for cand in candidates:
        if subprocess.run(["docker", "image", "inspect", cand], capture_output=True).returncode == 0:
            image = cand
            break
    if image is None:
        pytest.skip("No local Docker image available for sandbox bash test")

    agent = make_agent(
        {
            "type": "pi",
            "model": "fixture",
            "base_url": model_server[0],
            "key_env": "PI_TEST_KEY",
            "sandbox_image": image,
            "bash_timeout": 30,
            "timeout": 60,
        },
        PROJECT,
    )

    # 1. User role never receives the bash tool even when sharing a sandbox_image-configured instance.
    user_ws = tmp_path / "user_ws"
    user_ws.mkdir()
    (user_ws / "note.txt").write_text("hello", encoding="utf-8")
    model_server[2][0] = lambda _: "Received"
    agent.run(Call("user", "Inspect", user_ws, tmp_path / "user_log"))
    user_tools = [t["function"]["name"] for t in model_server[1][-1]["tools"]]
    assert "bash" not in user_tools

    # 2. Reviewer role gets offline read-only container bash; scripts write to scratch/ while submission/ stays read-only.
    rev_ws = tmp_path / "review_ws"
    (rev_ws / "submission").mkdir(parents=True)
    (rev_ws / "evaluation").mkdir(parents=True)
    (rev_ws / "scratch").mkdir(parents=True)
    (rev_ws / "submission/nums.txt").write_text("19\n28\n", encoding="utf-8")

    def rev_respond(payload):
        last = payload["messages"][-1]
        if last["role"] != "tool":
            return {
                "name": "bash",
                "arguments": json.dumps(
                    {
                        "command": "echo 'import pathlib; nums=[int(x) for x in pathlib.Path(\"submission/nums.txt\").read_text().split()]; print(\"SUM=\", sum(nums))' > scratch/sum.py && python3 scratch/sum.py"
                    }
                ),
            }
        assert "SUM= 47" in last["content"]
        return '{"reason":"Confirmed sum=47 via sandbox calculation","criteria":[]}'

    model_server[2][0] = rev_respond
    reply = agent.run(Call("reviewer", "Recompute total", rev_ws, tmp_path / "rev_log", schema=REVIEW_SCHEMA))
    assert "47" in json.loads(reply.text)["reason"]
    assert (rev_ws / "scratch/sum.py").is_file()
    assert (rev_ws / "submission/nums.txt").read_text(encoding="utf-8") == "19\n28\n"
