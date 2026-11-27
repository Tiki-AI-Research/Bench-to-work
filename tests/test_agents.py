"""Verify adapter protocol boundaries without requiring live external model endpoints."""

import json
import os
import sys
from pathlib import Path

import pytest

from multi_round import agents
from multi_round.agents import codex
from multi_round.agents.codex import CodexAgent
from multi_round.agents.command import CommandAgent
from multi_round.models import REVIEW_SCHEMA, AdapterError, Attachment, Call
from multi_round.processes import run_process


def test_framework_loads_external_factory(tmp_path):
    """External factories load in a clean subprocess without registering types or subclassing."""
    code = """
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
sys.path.insert(0, str(Path(sys.argv[1]) / "src"))

from multi_round.agents import make_agent
from multi_round.models import Call, Reply

plugin = ModuleType("external_worker")
def create_agent(project, *, prefix):
    assert project == Path(sys.argv[1]).resolve()
    return SimpleNamespace(run=lambda call: Reply(prefix + call.prompt))
plugin.create_agent = create_agent
sys.modules[plugin.__name__] = plugin
agent = make_agent({"type": "python", "factory": "external_worker:create_agent",
                    "prefix": "external:"}, Path(sys.argv[1]))
reply = agent.run(Call("worker", "request", Path.cwd(), Path.cwd()))
assert reply.text == "external:request"
"""
    run_process(
        [sys.executable, "-I", "-c", code, str(Path(__file__).resolve().parents[1])],
        cwd=tmp_path,
        stdin="",
        timeout=10,
        log_dir=tmp_path / "logs",
    )


def test_codex_initial_resume_preserves_environment(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setenv("CODEX_API_KEY", "test-key-not-real")

    def process(argv, **kwargs):
        seen.append((argv, kwargs))
        Path(argv[argv.index("-o") + 1]).write_text("Done", encoding="utf-8")
        return '{"type":"thread.started","thread_id":"owned-session"}\n{"type":"turn.completed"}'

    monkeypatch.setattr(codex, "run_process", process)
    agent = CodexAgent()
    first = agent.run(
        Call("worker", "Turn 1", tmp_path, tmp_path / "first", instructions="Initial rules")
    )
    agent.run(
        Call(
            "worker",
            "Turn 2",
            tmp_path,
            tmp_path / "second",
            first.session_id,
            history=[{"role": "user", "content": "History backup—must not be resent"}],
            instructions="Initial rules",
        )
    )
    assert "--sandbox" in seen[0][0] and "workspace-write" in seen[0][0]
    assert seen[1][0][2:4] == ["resume", "owned-session"]
    assert "--last" not in seen[1][0]
    assert seen[1][1]["stdin"] == "Turn 2"
    assert seen[0][1]["stdin"] == "Turn 1"
    for args, _ in seen:
        assert 'developer_instructions="Initial rules"' in args
    assert "env" not in seen[0][1]  # Do not override Codex startup environment.
    assert os.environ["CODEX_API_KEY"] == "test-key-not-real"
    with pytest.raises(AdapterError, match="Initial instructions changed"):
        agent.run(
            Call(
                "worker",
                "Turn 3",
                tmp_path,
                tmp_path / "third",
                first.session_id,
                instructions="Changed initial rules",
            )
        )
    assert len(seen) == 2


@pytest.mark.parametrize("role", ["user", "reviewer"])
def test_codex_non_worker_is_read_only_ephemeral_and_schema_constrained(
    tmp_path, monkeypatch, role
):
    def process(argv, **kwargs):
        assert "--ephemeral" in argv and "read-only" in argv
        if role == "user":
            assert "--output-schema" not in argv
        else:
            assert (
                json.loads(
                    Path(argv[argv.index("--output-schema") + 1]).read_text(
                        encoding="utf-8"
                    )
                )
                == REVIEW_SCHEMA
            )
        Path(argv[argv.index("-o") + 1]).write_text("Hello", encoding="utf-8")
        return '{"type":"turn.completed"}'

    monkeypatch.setattr(codex, "run_process", process)
    result = CodexAgent().run(
        Call(
            role,
            "Request",
            tmp_path,
            tmp_path / role,
            schema=REVIEW_SCHEMA if role == "reviewer" else None,
        )
    )
    assert result.text == "Hello"


def test_codex_requires_terminal_event(tmp_path, monkeypatch):
    def process(argv, **kwargs):
        Path(argv[argv.index("-o") + 1]).write_text("Incomplete message", encoding="utf-8")
        return '{"type":"thread.started","thread_id":"session"}'

    monkeypatch.setattr(codex, "run_process", process)
    with pytest.raises(AdapterError, match="turn.completed"):
        CodexAgent().run(Call("worker", "Execute", tmp_path, tmp_path / "logs"))


def test_command_protocol_round_trip(tmp_path):
    code = 'import json,sys; p=json.load(sys.stdin); print(json.dumps({"text":p["prompt"],"session_id":"s"}))'
    agent = CommandAgent([sys.executable, "-c", code])
    reply = agent.run(Call("worker", "request with café and spaces", tmp_path, tmp_path / "logs"))
    assert reply.text == "request with café and spaces" and reply.session_id == "s"


def test_external_process_timeout_is_reported(tmp_path):
    with pytest.raises(AdapterError, match="timed out"):
        run_process(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            cwd=tmp_path,
            stdin="",
            timeout=0.1,
            log_dir=tmp_path / "timeout",
        )


@pytest.mark.parametrize(
    "response",
    [{}, {"text": "   "}, {"text": False}, {"text": "Done", "session_id": 123}],
)
def test_invalid_command_protocol_does_not_succeed(tmp_path, response):
    code = "print(" + repr(json.dumps(response)) + ")"
    agent = CommandAgent([sys.executable, "-c", code])
    with pytest.raises(ValueError):
        agent.run(Call("worker", "hello", tmp_path, tmp_path / "logs"))


def test_command_gets_separate_history_and_instructions(tmp_path):
    code = 'import json,sys; p=json.load(sys.stdin); print(json.dumps({"text":json.dumps(p)}))'
    agent = CommandAgent([sys.executable, "-c", code])
    history = [
        {"role": "user", "content": "Prior turn"},
        {"role": "assistant", "content": "Prior reply"},
    ]
    reply = agent.run(
        Call(
            "worker",
            "Current turn",
            tmp_path,
            tmp_path / "logs",
            history=history,
            instructions="Initial rules",
        )
    )
    request = json.loads(reply.text)
    assert request["prompt"] == "Current turn"
    assert request["history"] == history
    assert request["instructions"] == "Initial rules"


def test_codex_does_not_fall_back_to_replaying_history(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Must not launch a fresh session disguised as a resume")

    monkeypatch.setattr(codex, "run_process", unexpected)
    with pytest.raises(AdapterError, match="missing native session_id"):
        CodexAgent().run(
            Call(
                "worker",
                "Turn 2",
                tmp_path,
                tmp_path / "logs",
                history=[{"role": "user", "content": "Turn 1"}],
            )
        )


def test_codex_requires_worker_session_id(tmp_path, monkeypatch):
    def process(argv, **kwargs):
        Path(argv[argv.index("-o") + 1]).write_text("Done", encoding="utf-8")
        return '{"type":"turn.completed"}'

    monkeypatch.setattr(codex, "run_process", process)
    with pytest.raises(AdapterError, match="did not return a thread_id"):
        CodexAgent().run(Call("worker", "Turn 1", tmp_path, tmp_path / "logs"))


def test_codex_rejects_text_only_before_launch(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Text-only calls must not fall back to a tool-enabled session")

    monkeypatch.setattr(codex, "run_process", unexpected)
    with pytest.raises(AdapterError, match="progress_reviewer"):
        CodexAgent().run(Call("reviewer", "Check submission?", None, tmp_path / "logs"))


def test_command_requires_explicit_text_only_support(tmp_path):
    code = 'import json,sys; p=json.load(sys.stdin); print(json.dumps({"text":json.dumps(p)}))'
    call = Call("reviewer", "Check submission?", None, tmp_path / "logs")
    with pytest.raises(AdapterError, match="text-only support"):
        CommandAgent([sys.executable, "-c", code]).run(call)
    request = json.loads(
        CommandAgent([sys.executable, "-c", code], supports_text_only=True)
        .run(call)
        .text
    )
    assert request["workspace"] is None
    assert request["session_id"] is None


def test_command_transfers_input_attachments(tmp_path):
    code = (
        "import json,sys; p=json.load(sys.stdin); "
        'print(json.dumps({"text":json.dumps(p),"trace_refs":["test:trace"]}))'
    )
    incoming = [
        Attachment(str(tmp_path / "data.csv")),
        Attachment(str(tmp_path / "in.png"), "image"),
    ]
    reply = CommandAgent([sys.executable, "-c", code]).run(
        Call("worker", "Process attachments", tmp_path, tmp_path / "logs", attachments=incoming)
    )
    assert json.loads(reply.text)["attachments"] == [
        {"path": str(tmp_path / "data.csv"), "kind": "file"},
        {"path": str(tmp_path / "in.png"), "kind": "image"},
    ]
    assert reply.trace_refs == ["test:trace"]


def test_codex_attaches_images_on_initial_and_resumed_calls(tmp_path, monkeypatch):
    calls = []

    def process(argv, **kwargs):
        calls.append((argv, kwargs))
        Path(argv[argv.index("-o") + 1]).write_text("Done", encoding="utf-8")
        return (
            '{"type":"thread.started","thread_id":"session"}\n{"type":"turn.completed"}'
        )

    monkeypatch.setattr(codex, "run_process", process)
    agent = CodexAgent()
    image = Attachment(str(tmp_path / "input.png"), "image")
    file = Attachment(str(tmp_path / "input.csv"))
    agent.run(
        Call("worker", "Turn 1", tmp_path, tmp_path / "a", attachments=[image, file])
    )
    agent.run(
        Call(
            "worker",
            "Turn 2",
            tmp_path,
            tmp_path / "b",
            session_id="session",
            attachments=[image],
        )
    )
    assert calls[1][0][2:4] == ["resume", "session"]
    for argv, kwargs in calls:
        assert argv[argv.index("--image") + 1] == image.path
        assert image.path in kwargs["stdin"]
    assert file.path in calls[0][1]["stdin"]
    assert file.path not in calls[1][1]["stdin"]  # Do not resend prior turn's file list.


def test_codex_project_executable_keeps_config_portable(tmp_path):
    config = {"type": "codex", "executable": "{project}/scripts/custom_codex.py"}
    agent = agents.make_agent(config, tmp_path)
    assert agent.executable == str(tmp_path / "scripts/custom_codex.py")
    assert config["executable"] == "{project}/scripts/custom_codex.py"


@pytest.mark.parametrize("provider,expected", [(None, None), ("gateway", 2)])
def test_codex_retry_ownership_is_explicit(tmp_path, monkeypatch, provider, expected):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "native-state"))
    seen = []

    def process(argv, **kwargs):
        seen.extend(argv)
        Path(argv[argv.index("-o") + 1]).write_text("Done")
        return '{"type":"turn.completed"}'

    monkeypatch.setattr(codex, "run_process", process)
    CodexAgent(provider=provider).run(
        Call("reviewer", "Evaluate", tmp_path, tmp_path / "logs")
    )
    settings = json.loads((tmp_path / "logs/native_settings.json").read_text())
    assert settings["api_retries"] == 2
    assert settings["isolated_config"] is True
    if provider:
        assert "model_providers.gateway.request_max_retries=0" in seen
        assert "model_providers.gateway.stream_max_retries=2" in seen
    else:
        assert settings["provider"]
    assert "--ignore-user-config" in seen
    system = (tmp_path / "logs/system.md").read_text()
    assert "formal stage evaluation" in system and "role-play the user" not in system
    assert 'developer_instructions=""' in seen


def test_codex_retry_cannot_guess_provider():
    with pytest.raises(ValueError, match="provider"):
        CodexAgent(api_retries=2)


def test_codex_isolated_roles_keep_provider_auth_and_model_budgets(
    tmp_path, monkeypatch
):
    state = tmp_path / "native"
    state.mkdir()
    (state / "config.toml").write_text(
        'model = "configured-model"\nmodel_provider = "custom"\nmodel_context_window = 262144\ndeveloper_instructions = "must not inherit"\n[model_providers.custom]\nname = "my-gateway"\nbase_url = "https://example.invalid/v1"\nenv_key = "MY_MODEL_KEY"\nwire_api = "responses"\n[model_providers.custom.env_http_headers]\nX-Account = "ACCOUNT_ENV"\n[mcp_servers.ambient]\ncommand = "do-not-start"\n'
    )
    monkeypatch.setenv("CODEX_HOME", str(state))
    settings = CodexAgent()._isolated_model_settings()
    assert settings["model_provider"] == "custom"
    assert settings["model"] == "configured-model"
    assert settings["model_context_window"] == 262144
    assert settings["model_providers.custom.env_key"] == "MY_MODEL_KEY"
    assert (
        settings['model_providers.custom.env_http_headers."X-Account"'] == "ACCOUNT_ENV"
    )
    assert not any("mcp" in key or "instructions" in key for key in settings)
