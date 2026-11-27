"""End-to-end offline regression using the real multi-round Runner + deterministic smoke_agent.py."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from . import REPO, SPEC_DIR
from .job import PUBLIC_TASK, WORKER_ROOT, mirror_compose
from .rewards import last_evaluations, stage_scores
from .tasks import find, resolve

AGENT = Path(__file__).resolve().parent / "smoke_agent.py"
BOX = "/tmp/btw-smoke"


def config() -> dict:
    command = [sys.executable, "-B", str(AGENT)]
    return {
        "mode": "Offline smoke test (deterministic test doubles; no LLM calls)",
        "user": {"type": "command", "supports_text_only": True, "command": command, "timeout": 120},
        "worker": {"type": "command", "command": command, "timeout": 120},
        "reviewer": {"type": "command", "supports_text_only": True, "command": command, "timeout": 120},
        "verifier_timeout": 300,
    }


def smoke(task_dir: Path, out: Path) -> dict:
    if out.exists():
        raise FileExistsError(f"Smoke output directory already exists: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    cfg = out.parent / f"{out.name}.config.json"
    with cfg.open("x", encoding="utf-8") as fh:
        fh.write(json.dumps(config(), ensure_ascii=False, indent=2))
    env = {
        **os.environ,
        "BTW_SMOKE_REFERENCE": str(task_dir / SPEC_DIR / "verify" / "reference"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    p = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "multi_round.cli",
            "--task",
            str(task_dir / SPEC_DIR / "task.json"),
            "--config",
            str(cfg),
            "--output",
            str(out),
        ],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        timeout=3600,
    )
    if not (out / "report.json").is_file():
        raise RuntimeError(f"Runner did not produce report.json (exit {p.returncode}): {p.stderr.decode('utf-8', errors='replace')[-1500:]}")
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    if p.returncode != 0 and report.get("status") == "completed":
        raise RuntimeError(
            f"Runner exited with code {p.returncode} despite completed report: "
            f"{p.stderr.decode('utf-8', errors='replace')[-1500:]}"
        )
    task = json.loads((out / "task.json").read_text(encoding="utf-8"))
    return summary(task_dir.name, report, task, out, stage_scores(report, task, rollout_dir=out) if report["status"] != "error" else None)


def harbor_smoke(task_dir: Path, out: Path) -> dict:
    reference = task_dir / SPEC_DIR / "verify" / "reference"
    files = [{"source": os.path.relpath(AGENT, REPO), "target": f"{BOX}/smoke_agent.py"}]
    if reference.is_dir():
        files += [
            {"source": os.path.relpath(p, REPO), "target": f"{BOX}/reference/{p.relative_to(reference).as_posix()}"}
            for p in sorted(reference.rglob("*"))
            if p.is_file()
        ]
    command = [sys.executable, "-B", str(AGENT)]
    cfg = {
        "mode": "Offline Harbor smoke test (Worker test double inside task Docker container; no LLM calls)",
        "user": {"type": "command", "supports_text_only": True, "command": command, "timeout": 120},
        "reviewer": {"type": "command", "supports_text_only": True, "command": command, "timeout": 120},
        "worker": {"command": ["env", f"BTW_SMOKE_REFERENCE={BOX}/reference", "python3", f"{BOX}/smoke_agent.py"], "timeout": 300},
        "workspace": {"root": WORKER_ROOT, "python": "python3", "public_task": PUBLIC_TASK},
        "public_files": files,
        "verifier_timeout": 300,
    }
    out.mkdir(parents=True)
    (out / "agent-config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    env_cfg: dict = {"type": "docker", "delete": True}
    if extra_compose := mirror_compose(out):
        env_cfg["extra_docker_compose"] = [str(p) for p in extra_compose]
    job = {
        "job_name": out.name,
        "jobs_dir": str(out / "jobs"),
        "n_attempts": 1,
        "n_concurrent_trials": 1,
        "datasets": [{"path": str(task_dir.parent.resolve()), "task_names": [task_dir.name]}],
        "agents": [
            {
                "import_path": "bench_to_work.agent:TaskAgent",
                "kwargs": {"project": str(REPO), "config_path": str(out / "agent-config.json")},
            }
        ],
        "environment": env_cfg,
        "verifier": {
            "import_path": "multi_round_harbor.verifier:ReportVerifier",
            "kwargs": {"reward_function": "bench_to_work.rewards:stage_scores"},
        },
    }
    (out / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    harbor = shutil.which("harbor", path=str(Path(sys.executable).parent)) or "harbor"
    env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "TMPDIR": os.path.realpath(tempfile.gettempdir()),
    }
    log_path = out / "harbor.log"
    with log_path.open("wb") as log_fh:
        p = subprocess.run(
            [harbor, "run", "--config", str(out / "job.json"), "--yes"],
            cwd=str(REPO),
            env=env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            timeout=7200,
        )
    (out / "harbor_exit.json").write_text(json.dumps({"returncode": p.returncode}) + "\n", encoding="utf-8")
    trials = [d for d in (out / "jobs" / out.name).glob("*") if (d / "rollout" / "report.json").is_file()] if (out / "jobs").is_dir() else []
    if not trials:
        raise RuntimeError(f"Harbor did not produce rollout/report.json (exit {p.returncode}); inspect {log_path}")
    trial = trials[0]
    report = json.loads((trial / "rollout" / "report.json").read_text(encoding="utf-8"))
    task = json.loads((trial / "rollout" / "task.json").read_text(encoding="utf-8"))
    result = json.loads((trial / "result.json").read_text(encoding="utf-8"))
    r = summary(task_dir.name, report, task, trial, (result.get("verifier_result") or {}).get("rewards"))
    r["harbor_returncode"] = p.returncode
    r["harbor_log"] = str(log_path)
    r["exception"] = (result.get("exception_info") or {}).get("exception_message")
    return r


def summary(name: str, report: dict, task: dict, run_dir: Path, rewards: dict | None) -> dict:
    last = last_evaluations(report)
    stages = []
    for s, log in zip(task["stages"], report["stages"]):
        program = {c["id"] for c in s["verifier_checks"]}
        ev = last.get(s["id"], [])
        failed = [c["id"] for c in ev if not c["passed"]]
        stages.append(
            {
                "id": s["id"],
                "status": log["status"],
                "turns": len(log["turns"]),
                "checks": f"{sum(1 for c in ev if c['id'] in program and c['passed'])}/{len(program)}",
                "failed": failed,
            }
        )
    hooks = [h for h in report["hooks"] if h["status"] != "completed"]
    return {
        "task": name,
        "status": report["status"],
        "reason": report.get("reason"),
        "stages": stages,
        "rewards": rewards,
        "hook_errors": [f"{h['id']}: {h.get('error')}" for h in hooks],
        "run_dir": str(run_dir),
    }


def ok(r: dict) -> bool:
    return (
        r.get("harbor_returncode", 0) == 0
        and r["status"] == "completed"
        and not r["hook_errors"]
        and not r.get("exception")
        and r["rewards"] is not None
        and r["rewards"].get("reward") == 1.0
        and r["rewards"].get("task_passed") == 1.0
        and all(s["turns"] == 1 and not s["failed"] for s in r["stages"])
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="End-to-end offline regression using real Runner + deterministic test doubles")
    ap.add_argument("tasks", nargs="*", help="Task ID(s) to run; omit to run all tasks")
    ap.add_argument("--harbor", action="store_true", help="Run via Harbor + Docker container")
    ap.add_argument("--dataset", default=str(REPO / "tasks"))
    args = ap.parse_args(argv)
    dataset = Path(args.dataset)
    try:
        dirs = [resolve(dataset, t) for t in args.tasks] if args.tasks else find(dataset)
    except (FileNotFoundError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if not dirs:
        print(f"No tasks found in dataset ({dataset})", file=sys.stderr)
        return 2
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    code = 0
    for d in dirs:
        run_name = f"{d.name}-{stamp}-{uuid4().hex[:8]}"
        try:
            if args.harbor:
                r = harbor_smoke(d, REPO / ".workspace" / "smoke-harbor" / run_name)
            else:
                r = smoke(d, REPO / ".workspace" / "smoke" / run_name)
        except (FileExistsError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"{d.name}: FAILED to execute: {exc}")
            code = 1
            continue
        stages = ", ".join(f"{s['id']} {s['checks']}" + (f" (failed {s['failed']})" if s["failed"] else "") for s in r["stages"])
        rewards = ", ".join(f"{k}={v:.3f}" for k, v in (r["rewards"] or {}).items())
        print(f"{r['task']}: {r['status']}; checks [{stages}]; rewards [{rewards or 'none'}]; {'PASSED' if ok(r) else 'FAILED'}")
        if r["status"] != "completed":
            print(f"  reason: {r['reason']}")
        for h in r["hook_errors"]:
            print(f"  hook error: {h}")
        if r.get("harbor_returncode", 0) != 0:
            print(f"  harbor exit code: {r['harbor_returncode']} (see {r.get('harbor_log')})")
        if r.get("exception"):
            print(f"  harbor exception: {r['exception'][:500]}")
        print(f"  run_dir: {r['run_dir']}")
        code = code or (0 if ok(r) else 1)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
