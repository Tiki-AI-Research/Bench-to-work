"""Compile a benchmark job YAML (configs/<name>.yaml) into Harbor job.json and Runner agent-config.json."""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml

from . import REPO, SPEC_DIR
from .tasks import find, resolve

WORKER_ROOT = "/workspace"
PUBLIC_TASK = "/opt/task-public"
HARNESSES = ("codex", "claude-code", "command")
MIRROR_BUILD_ARGS = ("DEBIAN_MIRROR", "DEBIAN_SECURITY_MIRROR", "NPM_REGISTRY", "PIP_INDEX_URL")
LLM_PYTHON_FACTORIES = {
    "bench_to_work.agents:make_review",
    "bench_to_work.agents:make_text",
}
DEFAULTS = {
    "task_names": [],
    "exclude_task_names": [],
    "n_attempts": 1,
    "n_concurrent_trials": 2,
    "jobs_dir": "jobs",
    "reward": "bench_to_work.rewards:stage_scores",
    "verifier_timeout": 300,
    "reviewer_protocol_retry_limit": 2,
}
CLI_WORKER_DEFAULTS = {
    "effort": None,
    "base_url": "https://api.openai.com/v1",
    "key_env": "OPENAI_API_KEY",
    "key_file": None,
    "env_vars": [],
    "files": [],
    "timeout": 3600,
}
COMMAND_WORKER_DEFAULTS = {
    "key_env": None,
    "key_file": None,
    "env_vars": [],
    "files": [],
    "timeout": 3600,
}


UNWRITABLE_CONTAINER_PREFIXES = ("/opt", "/usr", "/etc", "/bin", "/sbin", "/lib", "/lib64", "/var", "/root", "/proc", "/sys", "/dev", "/logs")


def _validate_single_worker_source(path: Path, src: Path, repo_resolved: Path, raw_label: object) -> str:
    if src.is_symlink():
        raise ValueError(f"{path}: worker.files must not include symlinks: {raw_label}")
    resolved = src.resolve()
    if not resolved.is_relative_to(repo_resolved):
        raise ValueError(f"{path}: worker.files source escapes repository root: {raw_label}")
    rel = resolved.relative_to(repo_resolved)
    parts = rel.parts
    if ".git" in parts or (parts and parts[0].startswith(".env")):
        raise ValueError(f"{path}: worker.files must not upload repository metadata or .env files: {rel.as_posix()}")
    if "tasks" in parts and SPEC_DIR in parts:
        raise ValueError(f"{path}: worker.files must not reference hidden task {SPEC_DIR}/ files: {rel.as_posix()}")
    return rel.as_posix()


def _validate_worker_target(path: Path, target_str: str) -> PurePosixPath:
    target = PurePosixPath(target_str)
    if not target.is_absolute() or ".." in target.parts:
        raise ValueError(f"{path}: worker.files target must be a normalized absolute container path: {target_str}")
    posix = target.as_posix()
    if posix == "/" or posix == WORKER_ROOT or posix.startswith(WORKER_ROOT + "/"):
        raise ValueError(
            f"{path}: worker.files target '{target_str}' must stay outside {WORKER_ROOT} (use e.g. /tmp/<agent>/...)"
        )
    if any(posix == p or posix.startswith(p + "/") for p in UNWRITABLE_CONTAINER_PREFIXES):
        raise ValueError(
            f"{path}: worker.files target '{target_str}' is not writable by container UID 1000; use /tmp/<agent>/..."
        )
    return target


def _validate_worker_files(path: Path, files: object) -> list[dict[str, str]]:
    if files is None:
        return []
    if not isinstance(files, list):
        raise ValueError(f"{path}: worker.files must be a list of {{source, target}} mappings")
    repo_resolved = REPO.resolve()
    out: list[dict[str, str]] = []
    for item in files:
        if not isinstance(item, dict) or not item.get("source") or not item.get("target"):
            raise ValueError(f"{path}: each worker.files entry must define non-empty 'source' and 'target'")
        raw_src = REPO / str(item["source"])
        if raw_src.is_symlink():
            raise ValueError(f"{path}: worker.files must not include symlinks: {item['source']}")
        src = raw_src.resolve()
        if not src.is_relative_to(repo_resolved):
            raise ValueError(f"{path}: worker.files source escapes repository root: {item['source']}")
        if not src.exists():
            raise FileNotFoundError(f"{path}: worker.files source does not exist: {item['source']}")
        target = _validate_worker_target(path, str(item["target"]))
        if src.is_file():
            rel_posix = _validate_single_worker_source(path, raw_src, repo_resolved, item["source"])
            out.append({"source": rel_posix, "target": target.as_posix()})
        elif src.is_dir():
            _validate_single_worker_source(path, raw_src, repo_resolved, item["source"])
            for child in sorted(src.rglob("*")):
                if "__pycache__" in child.parts or child.suffix == ".pyc":
                    continue
                if child.is_symlink():
                    raise ValueError(f"{path}: worker.files directory contains symlink: {child}")
                if child.is_file():
                    rel_posix = _validate_single_worker_source(path, child, repo_resolved, child)
                    sub = child.relative_to(src).as_posix()
                    out.append({"source": rel_posix, "target": (target / sub).as_posix()})
    return out


def load(path: Path) -> dict:
    job = {**DEFAULTS, **(yaml.safe_load(path.read_text(encoding="utf-8")) or {})}
    for key in ("dataset", "worker", "roles"):
        if not job.get(key):
            raise ValueError(f"{path}: missing required key '{key}'")
    raw_w = dict(job["worker"])
    harness = raw_w.get("harness") or ("command" if "command" in raw_w else "codex")
    if harness not in HARNESSES:
        raise ValueError(f"{path}: worker.harness must be one of {', '.join(HARNESSES)}")
    if harness == "command":
        w = {**COMMAND_WORKER_DEFAULTS, **raw_w, "harness": "command"}
        cmd = w.get("command")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x for x in cmd):
            raise ValueError(f"{path}: worker.command must be a non-empty list of strings when harness='command'")
    else:
        w = {**CLI_WORKER_DEFAULTS, **raw_w, "harness": harness}
        if not w.get("model"):
            raise ValueError(f"{path}: missing worker.model")
        if not w.get("base_url"):
            raise ValueError(f"{path}: missing worker.base_url")
        if not w.get("key_env"):
            raise ValueError(f"{path}: missing worker.key_env")
    if w.get("key_file") and not w.get("key_env"):
        raise ValueError(
            f"{path}: worker.key_file requires worker.key_env so the container target variable is known"
        )
    if not isinstance(w.get("env_vars") or [], list) or not all(
        isinstance(v, str) and v.strip() for v in (w.get("env_vars") or [])
    ):
        raise ValueError(f"{path}: worker.env_vars must be a list of environment variable names")
    w["resolved_files"] = _validate_worker_files(path, w.get("files"))
    job["worker"] = w
    return job


def worker_command(w: dict) -> list[str]:
    if w["harness"] == "command":
        return list(w["command"])
    cmd = [
        "python3",
        "/opt/worker.py",
        "--harness",
        w["harness"],
        "--model",
        str(w["model"]),
        "--base-url",
        w["base_url"],
        "--key-env",
        w["key_env"],
    ]
    return cmd + (["--effort", str(w["effort"])] if w.get("effort") else [])


def worker_env_vars(w: dict) -> list[str]:
    names: list[str] = []
    if w.get("key_env"):
        names.append(str(w["key_env"]))
    for extra in w.get("env_vars") or []:
        names.append(str(extra))
    return list(dict.fromkeys(names))


RESPONSES_ONLY_TOOL_MODELS = ("gpt-6.1-terra", "gpt-6.2-sol")
NO_TEMPERATURE_CHAT_MODELS = ("gpt-6.2-sol",)
MAX_COMPLETION_TOKENS_CHAT_MODELS = ("gpt-6.1-terra", "gpt-6.2-sol")


def _matches_model_family(model: str, prefixes: tuple[str, ...]) -> bool:
    return any(model == p or model.startswith(p + "-") for p in prefixes)


def validate_role_protocols(roles: Mapping[str, object]) -> list[str]:
    """Fail fast before container startup on verified-incompatible official OpenAI protocol/parameter combinations."""
    issues: list[str] = []
    for role_name in ("user", "reviewer", "progress_reviewer"):
        cfg = roles.get(role_name)
        if not isinstance(cfg, Mapping):
            continue
        host = (urlsplit(str(cfg.get("base_url") or "")).hostname or "").lower()
        if host != "api.openai.com":
            continue
        raw_model = cfg.get("model")
        model = str(raw_model or "").lower()
        kind = cfg.get("type")
        factory = str(cfg.get("factory") or "")
        is_pi = kind == "pi" or (kind == "python" and factory == "bench_to_work.agents:make_review")
        is_chat = kind == "openai_compatible" or (kind == "python" and factory == "bench_to_work.agents:make_text")
        if (
            is_pi
            and _matches_model_family(model, RESPONSES_ONLY_TOOL_MODELS)
            and cfg.get("api", "openai-completions") != "openai-responses"
        ):
            issues.append(
                f"{role_name}: model '{raw_model}' on official api.openai.com requires "
                f"'api': 'openai-responses' for tool-equipped Pi roles"
            )
        if is_chat:
            if _matches_model_family(model, NO_TEMPERATURE_CHAT_MODELS) and cfg.get("temperature", 0) is not None:
                issues.append(
                    f"{role_name}: model '{raw_model}' on official api.openai.com rejects 'temperature'; "
                    f"set 'temperature': null"
                )
            if (
                _matches_model_family(model, MAX_COMPLETION_TOKENS_CHAT_MODELS)
                and cfg.get("max_tokens_field", "max_tokens") != "max_completion_tokens"
            ):
                issues.append(
                    f"{role_name}: model '{raw_model}' on official api.openai.com requires "
                    f"'max_tokens_field': 'max_completion_tokens'"
                )
    return issues


def agent_config(job: dict, roles: dict) -> dict:
    w = job["worker"]
    for role in ("user", "reviewer"):
        if role not in roles:
            raise ValueError(f"Roles config is missing '{role}'")
    if proto_issues := validate_role_protocols(roles):
        raise ValueError("Incompatible controller role configuration:\n  - " + "\n  - ".join(proto_issues))
    if w["harness"] == "command":
        worker_label = f"command ({' '.join(w['command'][:2])}, containerized)"
    else:
        worker_label = f"{w['harness']} + {w['model']} (effort={w.get('effort') or 'default'}, containerized)"
    worker_cfg: dict = {"command": worker_command(w), "timeout": int(w["timeout"])}
    if env_vars := worker_env_vars(w):
        worker_cfg["env_vars"] = env_vars
    if w.get("key_env") and w.get("key_file"):
        worker_cfg["key_files"] = {str(w["key_env"]): str(w["key_file"])}
    cfg = {
        "mode": f"Worker: {worker_label}; {roles.get('mode', 'Controller roles configured via roles file')}",
        **{k: v for k, v in roles.items() if k != "mode"},
        "worker": worker_cfg,
        "workspace": {"root": WORKER_ROOT, "python": "python3", "public_task": PUBLIC_TASK},
        "verifier_timeout": int(job["verifier_timeout"]),
        "reviewer_protocol_retry_limit": int(job["reviewer_protocol_retry_limit"]),
    }
    if w.get("resolved_files"):
        cfg["public_files"] = list(w["resolved_files"])
    return cfg


def _credential_present(key_env: str | None, key_file: str | None, env: Mapping[str, str]) -> bool:
    if key_env and env.get(key_env, "").strip():
        return True
    if key_file:
        p = Path(key_file).expanduser()
        if p.is_file() and p.read_text(encoding="utf-8").strip():
            return True
    return False


def _role_credential_specs(cfg: dict) -> list[tuple[str | None, str | None]]:
    kind = cfg.get("type")
    if kind == "command":
        return [(cfg.get("key_env"), cfg.get("key_file"))] if (cfg.get("key_env") or cfg.get("key_file")) else []
    if kind in {"pi", "openai_compatible"}:
        return [(cfg.get("key_env") or "OPENAI_API_KEY", cfg.get("key_file"))]
    if kind == "codex":
        return [(cfg.get("key_env"), cfg.get("key_file"))] if (cfg.get("key_env") or cfg.get("key_file")) else []
    if kind == "python":
        factory = cfg.get("factory") or ""
        if factory in LLM_PYTHON_FACTORIES or cfg.get("key_env") or cfg.get("key_file") or cfg.get("base_url"):
            return [(cfg.get("key_env") or "OPENAI_API_KEY", cfg.get("key_file"))]
    return []


def _format_missing(role: str, key_env: str | None, key_file: str | None) -> str:
    if key_env and key_file:
        return f"{role}: missing {key_env} (and key_file '{key_file}' is missing or empty)"
    if key_env:
        return f"{role}: missing {key_env}"
    return f"{role}: key_file '{key_file}' is missing or empty"


def missing_credentials(job: dict, roles: dict, env: Mapping[str, str] | None = None) -> list[str]:
    """Return human-readable descriptions of any missing Worker or controller-role credentials (never secret values)."""
    env_map = os.environ if env is None else env
    missing: list[str] = []
    w = job["worker"]
    primary_env = w.get("key_env")
    primary_file = w.get("key_file")
    if primary_file and not primary_env:
        missing.append(f"worker: key_file '{primary_file}' requires worker.key_env to name the container variable")
    elif primary_env:
        if not _credential_present(primary_env, primary_file, env_map):
            missing.append(_format_missing("worker", primary_env, primary_file))
    for var in w.get("env_vars") or []:
        if var != primary_env and not _credential_present(var, None, env_map):
            missing.append(_format_missing("worker", var, None))

    for role_name in ("user", "reviewer", "progress_reviewer"):
        if role_name not in roles:
            continue
        for key_env, key_file in _role_credential_specs(roles[role_name]):
            if not _credential_present(key_env, key_file, env_map):
                missing.append(_format_missing(role_name, key_env, key_file))
    return missing


def needs_pi_runtime(roles: dict) -> bool:
    for role_name in ("user", "reviewer", "progress_reviewer"):
        cfg = roles.get(role_name) or {}
        if cfg.get("type") == "pi":
            return True
        if cfg.get("type") == "python" and str(cfg.get("factory") or "").endswith(":make_review"):
            return True
    return False


def harbor_datasets(dataset: Path, tasks: list[str], exclude: list[str]) -> list[dict]:
    def hit(d: Path, patterns: list[str]) -> bool:
        return any(fnmatch(d.name, p) or fnmatch(d.relative_to(dataset).as_posix(), p) for p in patterns)

    dirs = find(dataset)
    if not dirs and not tasks:
        return [{"path": str(dataset)}]
    picked = [d for d in dirs if (not tasks or hit(d, tasks)) and not hit(d, exclude)]
    if not picked:
        raise ValueError(f"No tasks matched task_names={tasks}, exclude_task_names={exclude} in {dataset}")
    groups: dict[Path, list[str]] = {}
    for d in picked:
        groups.setdefault(d.parent.resolve(), []).append(d.name)
    return [{"path": str(path), "task_names": names} for path, names in groups.items()]


def harbor_job(
    job: dict,
    *,
    name: str,
    datasets: list[dict],
    config_path: Path,
    extra_compose: list[Path] | None = None,
) -> dict:
    env_cfg: dict = {"type": "docker", "delete": True}
    if extra_compose:
        env_cfg["extra_docker_compose"] = [str(p) for p in extra_compose]
    return {
        "job_name": name,
        "jobs_dir": str((REPO / job["jobs_dir"]).resolve()),
        "n_attempts": int(job["n_attempts"]),
        "n_concurrent_trials": int(job["n_concurrent_trials"]),
        "datasets": datasets,
        "agents": [
            {
                "import_path": "bench_to_work.agent:TaskAgent",
                "kwargs": {"project": str(REPO), "config_path": str(config_path)},
            }
        ],
        "environment": env_cfg,
        "verifier": {
            "import_path": "multi_round_harbor.verifier:ReportVerifier",
            "kwargs": {"reward_function": job["reward"]},
        },
    }


def mirror_compose(out: Path) -> list[Path]:
    """Generate a Docker Compose build-args override when optional mirror env vars are set."""
    build_args = {k: os.environ[k] for k in MIRROR_BUILD_ARGS if os.environ.get(k, "").strip()}
    if not build_args:
        return []
    compose_path = out / "build-mirrors.compose.yaml"
    compose_path.write_text(
        yaml.safe_dump({"services": {"main": {"build": {"args": build_args}}}}, sort_keys=False),
        encoding="utf-8",
    )
    return [compose_path]


def write(job_path: Path, tasks: list[str] | None = None, name: str | None = None, out_root: Path | None = None) -> Path:
    job = load(job_path)
    dataset = (REPO / job["dataset"]).resolve()
    if not dataset.is_dir():
        raise FileNotFoundError(f"Dataset directory does not exist: {dataset}")
    names = list(tasks) if tasks else list(job["task_names"])
    if tasks:
        missing = []
        for t in tasks:
            try:
                resolve(dataset, t)
            except FileNotFoundError:
                missing.append(t)
        if missing:
            raise FileNotFoundError(f"Tasks not found in dataset: {missing} ({dataset})")
    datasets = harbor_datasets(dataset, names, list(job["exclude_task_names"]))
    roles = json.loads((REPO / job["roles"]).read_text(encoding="utf-8"))
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    explicit_name = name is not None
    base_name = name or "-".join([job_path.stem, *([Path(tasks[0]).name] if tasks and len(tasks) == 1 else []), stamp])
    root_dir = out_root or REPO / ".workspace" / "jobs"
    name = base_name
    if not explicit_name:
        seq = 2
        while (root_dir / name).exists():
            name = f"{base_name}-{seq}"
            seq += 1
    out = root_dir / name
    out.mkdir(parents=True, exist_ok=False)
    config_path = out / "agent-config.json"
    config_path.write_text(json.dumps(agent_config(job, roles), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    extra_compose = mirror_compose(out)
    job_json = out / "job.json"
    job_json.write_text(
        json.dumps(
            harbor_job(job, name=name, datasets=datasets, config_path=config_path, extra_compose=extra_compose),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return job_json


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Compile job YAML into Harbor job.json and Runner agent-config.json")
    ap.add_argument("--job", required=True, help="Path to configs/<name>.yaml")
    ap.add_argument("--task", action="append", default=[], help="Run only specific task(s); repeatable")
    ap.add_argument("--name", help="Explicit job name")
    ap.add_argument("--check-credentials", action="store_true", help="Verify Worker and controller-role credentials without writing files")
    ap.add_argument("--needs-pi", action="store_true", help="Print 1 if controller roles require Node/Pi runtime, else 0")
    args = ap.parse_args(argv)
    try:
        job = load(Path(args.job))
        roles = json.loads((REPO / job["roles"]).read_text(encoding="utf-8"))
        if args.needs_pi:
            print("1" if needs_pi_runtime(roles) else "0")
            return 0
        if args.check_credentials:
            if proto_issues := validate_role_protocols(roles):
                raise ValueError("Incompatible controller role configuration:\n  - " + "\n  - ".join(proto_issues))
            missing = missing_credentials(job, roles)
            if missing:
                print(
                    "Missing required credentials for selected job and roles (configure in environment or .env):\n"
                    + "\n".join(f"  - {m}" for m in missing),
                    file=sys.stderr,
                )
                return 2
            return 0
        path = write(Path(args.job), args.task, args.name)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"Failed to build job config: {exc}", file=sys.stderr)
        return 2
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
