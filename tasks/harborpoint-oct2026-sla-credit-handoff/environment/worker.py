"""In-container Worker command wrapper (/opt/worker.py) implementing the JSON stdio Call/Reply protocol.

Bridges a single Worker turn to Codex CLI or Claude Code CLI inside the isolated task container:
- Starts a fresh native session on turn 1 and resumes by session_id on subsequent turns (never silently
  rebuilds a missing session from public chat history).
- Disables external web search / web fetch tools so evaluation measures reasoning over task materials.
- Writes native CLI event streams under /logs/agent/native/<call_id>/ for debugging only (never used for grading).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

NATIVE = Path("/logs/agent/native")
TOOLS = Path("/opt/worker-tools.json")
NO_REPLY = "(No assistant text emitted in this turn)"


def prompt_of(call: dict) -> str:
    prompt = call["prompt"]
    if not call.get("session_id") and call.get("instructions"):
        prompt = call["instructions"] + "\n\n" + prompt
    if call.get("attachments"):
        prompt += "\n\nAttachments:\n" + json.dumps(call["attachments"], ensure_ascii=False)
    return prompt


def tool_config(path: Path = TOOLS) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def mcp_servers(config: dict, harness: str) -> dict:
    return {
        name: {"command": s["command"], "args": [*s.get("args", []), *(s.get("args_by_harness") or {}).get(harness, [])]}
        for name, s in (config.get("servers") or {}).items()
    }


def codex_tool_args(config: dict) -> list[str]:
    out = []
    for name, s in mcp_servers(config, "codex").items():
        out += [
            "-c",
            f"mcp_servers.{name}.command={json.dumps(s['command'])}",
            "-c",
            f"mcp_servers.{name}.args={json.dumps(s['args'], ensure_ascii=False)}",
            "-c",
            f"mcp_servers.{name}.tool_timeout_sec=180",
        ]
        disabled = (config.get("disabled_tools") or {}).get(name)
        if disabled:
            out += ["-c", f"mcp_servers.{name}.disabled_tools={json.dumps(disabled)}"]
    return out


def claude_tool_args(config: dict) -> tuple[list[str], list[str]]:
    servers = mcp_servers(config, "claude-code")
    if not servers:
        return [], []
    spec = {"mcpServers": {name: {"type": "stdio", **s} for name, s in servers.items()}}
    denied = [f"mcp__{name}__{tool}" for name, tools in (config.get("disabled_tools") or {}).items() for tool in tools]
    return ["--mcp-config", json.dumps(spec, ensure_ascii=False), "--strict-mcp-config"], denied


def codex_argv(call: dict, args, output: Path) -> list[str]:
    argv = ["codex", "exec"]
    if call.get("session_id"):
        argv += ["resume", call["session_id"]]
    argv += [
        "--json",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        args.model,
        "-o",
        str(output),
        "-c",
        "features.multi_agent=false",
        "-c",
        'web_search="disabled"',
        "-c",
        'model_provider="gateway"',
        "-c",
        'model_providers.gateway.name="gateway"',
        "-c",
        f'model_providers.gateway.base_url="{args.base_url}"',
        "-c",
        'model_providers.gateway.wire_api="responses"',
        "-c",
        f'model_providers.gateway.env_key="{args.key_env}"',
    ]
    argv += codex_tool_args(tool_config())
    if args.effort:
        argv += ["-c", f'model_reasoning_effort="{args.effort}"']
    for attachment in call.get("attachments") or []:
        if attachment.get("kind") == "image":
            argv += ["--image", attachment["path"]]
    return argv + ["-"]


def run_codex(call: dict, args, directory: Path) -> dict:
    output = directory / "last_message.txt"
    env = dict(os.environ)
    env.pop("CODEX_API_KEY", None)
    with (directory / "events.jsonl").open("w", encoding="utf-8") as events:
        subprocess.run(
            codex_argv(call, args, output),
            input=prompt_of(call),
            text=True,
            encoding="utf-8",
            stdout=events,
            check=True,
            env=env,
            cwd=call["workspace"],
        )
    session, completed = call.get("session_id"), False
    for line in (directory / "events.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "thread.started":
            session = event.get("thread_id") or session
        if event.get("type") == "turn.completed":
            completed = True
    if not completed or not session or not output.is_file():
        raise SystemExit("Codex did not complete cleanly or failed to emit session_id")
    text = output.read_text(encoding="utf-8")
    return {
        "text": text if text.strip() else NO_REPLY,
        "session_id": session,
        "trace_refs": [f"harbor-agent:native/{call['call_id']}/events.jsonl"],
    }


def claude_argv(call: dict, args) -> list[str]:
    mcp, denied = claude_tool_args(tool_config())
    argv = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        args.model,
        "--permission-mode",
        "bypassPermissions",
        "--disallowedTools",
        ",".join(["WebSearch", "WebFetch", *denied]),
        *mcp,
    ]
    if args.effort:
        argv += ["--effort", args.effort]
    if call.get("session_id"):
        argv += ["--resume", call["session_id"]]
    return argv


def claude_env(call: dict, args) -> dict:
    run_key = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in (call.get("run_id") or "default"))
    home = Path(os.environ.get("HOME") or "/tmp")
    env = {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
        "CLAUDE_CONFIG_DIR": str(home / ".cc-runs" / run_key),
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": args.model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": args.model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": args.model,
        "ANTHROPIC_SMALL_FAST_MODEL": args.model,
        "CLAUDE_CODE_SUBAGENT_MODEL": args.model,
    }
    for key in ("PYTHONDONTWRITEBYTECODE", "TZ", "LC_ALL", "CLAUDE_CODE_MAX_CONTEXT_TOKENS"):
        if key in os.environ:
            env[key] = os.environ[key]
    if args.base_url:
        env["ANTHROPIC_BASE_URL"] = args.base_url
    token = os.environ.get(args.key_env or "")
    if not token:
        raise SystemExit(f"Environment variable {args.key_env} is empty or unset")
    env["ANTHROPIC_AUTH_TOKEN"] = token
    return env


def run_claude(call: dict, args, directory: Path) -> dict:
    env = claude_env(call, args)
    Path(env["CLAUDE_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
    result, init_session = None, None
    with (directory / "events.jsonl").open("w", encoding="utf-8") as events:
        proc = subprocess.Popen(
            claude_argv(call, args),
            cwd=call["workspace"],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        proc.stdin.write(prompt_of(call))
        proc.stdin.close()
        for line in proc.stdout:
            events.write(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                init_session = event.get("session_id")
            if event.get("type") == "result":
                result = event
        code = proc.wait()
    if result is None:
        raise SystemExit(f"Claude Code did not emit a terminal result event (exit code {code})")
    text = result.get("result") or ""
    if code or result.get("subtype") != "success" or result.get("is_error"):
        raise SystemExit(
            f"Claude Code did not complete cleanly: exit={code}, subtype={result.get('subtype')}, is_error={result.get('is_error')}"
        )
    session = result.get("session_id") or init_session
    if not session:
        raise SystemExit("Claude Code did not return a session_id")
    return {
        "text": text if text.strip() else NO_REPLY,
        "session_id": session,
        "trace_refs": [f"harbor-agent:native/{call['call_id']}/events.jsonl"],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Container Worker wrapper (Codex / Claude Code)")
    ap.add_argument("--harness", choices=("codex", "claude-code"), default="codex")
    ap.add_argument("--model", required=True)
    ap.add_argument("--effort", help="Reasoning effort level")
    ap.add_argument("--base-url", required=True, help="API base URL (with /v1 for Codex Responses API; without /v1 for Anthropic Messages API)")
    ap.add_argument("--key-env", default="OPENAI_API_KEY", help="Environment variable holding the API key")
    args = ap.parse_args(argv)
    call = json.load(sys.stdin)
    if call.get("role") != "worker" or call.get("workspace") is None:
        raise SystemExit("worker.py only handles Worker role calls with a workspace")
    if call.get("history") and not call.get("session_id"):
        raise SystemExit("Missing session_id on follow-up turn; refusing to silently rebuild session from history")
    directory = NATIVE / call["call_id"]
    directory.mkdir(parents=True)
    reply = (run_codex if args.harness == "codex" else run_claude)(call, args, directory)
    print(json.dumps(reply, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
