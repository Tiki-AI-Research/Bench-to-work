"""Unit tests for bench_to_work: task discovery, job generation, worker wrapper, rewards, and check/smoke."""
from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from unittest import mock

import pytest

import hashlib
import shutil
from bench_to_work import REPO, SPEC_DIR
from bench_to_work.agent import TaskAgent, spec_task
from bench_to_work.agents import TRANSIENT, CheckedReview, contradictions
from bench_to_work.check import TEMPLATES, check_answers, check_public, check_scoring, check_task, container_isolation_issues, main as check_main
from bench_to_work.job import PUBLIC_TASK, load as load_job, missing_credentials, write
from bench_to_work.rewards import score_rollout, stage_scores
from bench_to_work.smoke import main as smoke_main, ok, smoke
from bench_to_work.tasks import find, resolve
from multi_round.models import AdapterError, Call, ExternalExecutionError, Reply
from multi_round.workspaces import LocalWorkspace
from multi_round_harbor.agent import MainAgent
from multi_round_harbor.worker import HarborWorker

DATASET = REPO / "tasks"
TASK_DIRS = {p.name: p for p in find(DATASET)}
BUILTIN_TASKS = (
    "fy26-merit-promotion-calibration",
    "harborpoint-oct2026-sla-credit-handoff",
)

worker_spec = importlib.util.spec_from_file_location("worker_template", TEMPLATES / "worker.py")
worker = importlib.util.module_from_spec(worker_spec)
worker_spec.loader.exec_module(worker)


@pytest.mark.parametrize("task_id", BUILTIN_TASKS)
def test_builtin_benchmark_tasks_pass_offline_check(task_id):
    assert task_id in TASK_DIRS
    res = check_task(TASK_DIRS[task_id])
    assert res["problems"] == [], f"Unexpected check problems for {task_id}: {res['problems']}"
    assert len(res["answers"]) >= 4
    assert all(a == b and b > 0 for a, b in res["answers"].values())


@pytest.mark.parametrize("task_id", BUILTIN_TASKS)
def test_builtin_benchmark_tasks_pass_end_to_end_smoke(tmp_path, task_id):
    res = smoke(TASK_DIRS[task_id], tmp_path / f"smoke-{task_id}")
    assert ok(res), f"Smoke test failed for {task_id}: {res}"
    assert res["rewards"]["reward"] == pytest.approx(1.0)


def test_task_discovery_and_resolution(tmp_path):
    (tmp_path / "finance" / "t1").mkdir(parents=True)
    (tmp_path / "finance" / "t1" / "task.toml").write_text("", encoding="utf-8")
    (tmp_path / "raw_unimported").mkdir()
    assert [p.name for p in find(tmp_path)] == ["t1"]
    assert resolve(tmp_path, "t1") == tmp_path / "finance" / "t1"
    assert resolve(tmp_path, "finance/t1") == tmp_path / "finance" / "t1"
    with pytest.raises(FileNotFoundError):
        resolve(tmp_path, "missing")


def test_task_agent_resolves_spec_from_trial_config(tmp_path):
    sample = BUILTIN_TASKS[0]
    trial = tmp_path / "trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "config.json").write_text(
        json.dumps({"trial_name": "t__1", "task": {"path": str(TASK_DIRS[sample])}}),
        encoding="utf-8",
    )
    assert spec_task(trial) == (TASK_DIRS[sample] / SPEC_DIR / "task.json").resolve()

    agent = TaskAgent(
        logs_dir=trial / "agent",
        project=str(REPO),
        config_path=str(REPO / "configs" / "roles" / "pi-openai.json"),
    )
    seen = {}

    async def fake_setup(self, environment):
        seen["task_path"] = self.task_path

    with mock.patch.object(MainAgent, "setup", fake_setup):
        asyncio.run(agent.setup(environment=None))
    assert seen["task_path"] == (TASK_DIRS[sample] / SPEC_DIR / "task.json").resolve()


@pytest.mark.parametrize("cfg_name", ["job", "claude-code"])
def test_shipped_job_configs_compile_valid_harbor_jobs(tmp_path, cfg_name):
    job_json = write(REPO / "configs" / f"{cfg_name}.yaml", out_root=tmp_path)
    job = json.loads(job_json.read_text(encoding="utf-8"))
    assert job["agents"][0]["import_path"] == "bench_to_work.agent:TaskAgent"
    assert job["verifier"]["import_path"] == "multi_round_harbor.verifier:ReportVerifier"
    assert job["verifier"]["kwargs"]["reward_function"] == "bench_to_work.rewards:stage_scores"
    agent_cfg = json.loads((job_json.parent / "agent-config.json").read_text(encoding="utf-8"))
    assert agent_cfg["workspace"] == {"root": "/workspace", "python": "python3", "public_task": PUBLIC_TASK}
    assert {"user", "reviewer", "progress_reviewer"} <= set(agent_cfg)

    expected_tasks = sorted(TASK_DIRS.keys())
    code = (
        "import asyncio, json, sys\n"
        "from harbor.models.job.config import JobConfig\n"
        "j = JobConfig.model_validate(json.load(open(sys.argv[1], encoding='utf-8')))\n"
        "tasks = sorted(t.path.name for d in j.datasets for t in asyncio.run(d.get_task_configs(disable_verification=False)))\n"
        "expected = json.loads(sys.argv[2])\n"
        "assert tasks == expected and len(tasks) >= 2, (tasks, expected)\n"
    )
    subprocess.run([sys.executable, "-c", code, str(job_json), json.dumps(expected_tasks)], check=True)


def test_container_isolation_rejects_controller_placeholders_and_handlers():
    bad = {
        "hooks": [
            {"id": "h1", "event": "after_stage", "handler": "host_fn"},
            {"id": "h2", "event": "after_worker", "command": ["python3", "x.py", "{turn_dir}"]},
        ]
    }
    assert len(container_isolation_issues(bad)) == 2


def test_core_minor_rewards_and_contradiction_detection():
    task = {
        "id": "t",
        "stages": [
            {
                "id": "s1",
                "verifier_checks": [{"id": "c1"}, {"id": "m1"}, {"id": "m2"}],
                "criteria": [{"id": "c2"}],
            },
            {
                "id": "s2",
                "verifier_checks": [{"id": "c3"}],
                "criteria": [],
            },
        ],
    }
    design = {
        "scoring": {"rule": "core_minor", "minor_weight": 0.2},
        "stages": [
            {
                "id": "s1",
                "scored": True,
                "items": [
                    {"id": "c1", "role": "core"},
                    {"id": "c2", "role": "core"},
                    {"id": "m1", "role": "minor"},
                    {"id": "m2", "role": "minor"},
                ],
            },
            {
                "id": "s2",
                "scored": True,
                "items": [
                    {"id": "c3", "role": "core"},
                ],
            },
        ],
    }
    # Case 1: all `core` items pass across both stages, 1 of 2 `minor` items in s1 fails.
    # s1 = 0.8 + 0.2 * 0.5 = 0.9; s2 = 1.0 -> reward = 0.95, while stages_passed & task_passed check `core` ONLY (= 1.0).
    minor_miss = {
        "stages": [
            {
                "id": "s1",
                "turns": [
                    {
                        "evaluation": [
                            {"id": "c1", "passed": True},
                            {"id": "c2", "passed": True},
                            {"id": "m1", "passed": True},
                            {"id": "m2", "passed": False},
                        ]
                    }
                ],
            },
            {
                "id": "s2",
                "turns": [{"evaluation": [{"id": "c3", "passed": True}]}],
            },
        ]
    }
    res = stage_scores(minor_miss, task, design)
    assert res["reward"] == pytest.approx(0.95)
    assert res["stages_passed"] == pytest.approx(1.0)
    assert res["task_passed"] == pytest.approx(1.0)
    assert res["items"] == pytest.approx(0.8)

    # Case 2: a `core` item in s1 fails even though both `minor` items pass -> s1 = 0.0, task_passed = 0.0.
    core_miss = {
        "stages": [
            {
                "id": "s1",
                "turns": [
                    {
                        "evaluation": [
                            {"id": "c1", "passed": True},
                            {"id": "c2", "passed": False},
                            {"id": "m1", "passed": True},
                            {"id": "m2", "passed": True},
                        ]
                    }
                ],
            },
            {
                "id": "s2",
                "turns": [{"evaluation": [{"id": "c3", "passed": True}]}],
            },
        ]
    }
    res_core_miss = stage_scores(core_miss, task, design)
    assert res_core_miss["reward"] == pytest.approx(0.5)
    assert res_core_miss["stages_passed"] == pytest.approx(0.5)
    assert res_core_miss["task_passed"] == pytest.approx(0.0)
    assert res_core_miss["items"] == pytest.approx(0.8)

    bad_json = json.dumps({"reason": "ok", "criteria": [{"id": "c1", "passed": False, "evidence": "Meets every requirement. Verdict: PASS."}]})
    assert len(contradictions(bad_json)) == 1


def test_worker_template_disables_web_search_and_enforces_session_resume(monkeypatch):
    args = Namespace(harness="codex", model="gpt-6.2-sol", effort="high", base_url="https://api.openai.com/v1", key_env="OPENAI_API_KEY")
    call = {
        "role": "worker",
        "call_id": "c1",
        "run_id": "r1",
        "prompt": "Update workbook",
        "instructions": "System rules",
        "workspace": "/workspace",
        "session_id": None,
        "history": [],
        "attachments": [{"path": "/workspace/a.xlsx", "kind": "file"}],
    }
    joined = " ".join(worker.codex_argv(call, args, Path("/logs/out.txt")))
    assert 'web_search="disabled"' in joined and "features.multi_agent=false" in joined

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({**call, "history": [{"role": "user", "content": "hi"}]})))
    with pytest.raises(SystemExit, match="session_id"):
        worker.main(["--model", "m", "--base-url", "https://api.openai.com/v1"])


def _write_manifest(spec_dir: Path) -> None:
    files = {
        p.relative_to(spec_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(spec_dir.rglob("*"))
        if p.is_file() and p.name != "manifest.json" and "__pycache__" not in p.parts
    }
    (spec_dir / "manifest.json").write_text(json.dumps({"files": files}, indent=2) + "\n", encoding="utf-8")


def test_check_fails_when_required_reference_dir_is_deleted(tmp_path):
    src = TASK_DIRS["harborpoint-oct2026-sla-credit-handoff"]
    copied = tmp_path / "tasks" / src.name
    shutil.copytree(src, copied)
    shutil.rmtree(copied / SPEC_DIR / "verify" / "reference" / "s2_sre_overlap")
    _write_manifest(copied / SPEC_DIR)

    code = check_main(["--dataset", str(tmp_path / "tasks")])
    assert code != 0
    res = check_task(copied)
    assert any("s2_sre_overlap" in p and "missing required reference directory" in p for p in res["problems"])


@pytest.mark.parametrize(
    "returned_checks,expected_substr",
    [
        ([], "missing=['chk_a', 'chk_b']"),
        ([{"id": "chk_a", "passed": True}], "missing=['chk_b']"),
        (
            [
                {"id": "chk_a", "passed": True},
                {"id": "chk_a", "passed": True},
                {"id": "chk_b", "passed": True},
            ],
            "duplicate=['chk_a']",
        ),
    ],
)
def test_check_answers_rejects_empty_missing_or_duplicate_verifier_ids(tmp_path, returned_checks, expected_substr):
    task_dir = tmp_path / "synthetic-task"
    spec_dir = task_dir / SPEC_DIR
    (spec_dir / "verify" / "reference" / "s1").mkdir(parents=True)
    # s2 has only semantic criteria and no verifier_checks -> allowed to have no reference directory.
    task = {
        "id": "synthetic-task",
        "stages": [
            {"id": "s1", "initial": {}, "verifier_checks": [{"id": "chk_a"}, {"id": "chk_b"}], "criteria": []},
            {"id": "s2", "initial": {}, "verifier_checks": [], "criteria": [{"id": "crit_only"}]},
        ],
    }
    verifier_py = (
        "import json\n"
        f"print(json.dumps({{'checks': {returned_checks!r}}}))\n"
    )
    (spec_dir / "verifier.py").write_text(verifier_py, encoding="utf-8")
    problems, scores = check_answers(task_dir, task, tmp_path / "work")
    assert len(problems) == 1
    assert "synthetic-task/s1" in problems[0]
    assert expected_substr in problems[0]
    assert "s2" not in scores


def test_custom_worker_job_dry_run_and_two_turn_session_persistence(tmp_path):
    job_json = write(REPO / "configs" / "custom-worker.yaml", out_root=tmp_path)
    job = json.loads(job_json.read_text(encoding="utf-8"))
    agent_cfg = json.loads((job_json.parent / "agent-config.json").read_text(encoding="utf-8"))
    assert agent_cfg["worker"]["command"] == ["python3", "/tmp/custom-worker/agent.py"]
    assert "env_vars" not in agent_cfg["worker"]
    assert agent_cfg["public_files"] == [
        {"source": "examples/custom_worker/agent.py", "target": "/tmp/custom-worker/agent.py"}
    ]
    assert job["datasets"][0]["task_names"] == ["harborpoint-oct2026-sla-credit-handoff"]

    # Reject worker.files attempting to upload hidden task spec/ files (directly, via parent dir recursion, or via symlink),
    # or targeting unwritable /opt or /workspace paths.
    for bad_snippet, match_re in [
        ("    - source: tasks/harborpoint-oct2026-sla-credit-handoff/spec/task.json\n      target: /tmp/x.json\n", "spec/"),
        ("    - source: tasks\n      target: /tmp/all_tasks\n", "spec/"),
        ("    - source: examples/custom_worker/agent.py\n      target: /opt/custom-worker/agent.py\n", "UID 1000"),
        ("    - source: examples/custom_worker/agent.py\n      target: /workspace/agent.py\n", "/workspace"),
    ]:
        bad_yaml = tmp_path / "bad.yaml"
        bad_yaml.write_text(
            "dataset: tasks\nroles: configs/roles/offline-smoke.json\nworker:\n"
            "  harness: command\n  command: [python3, /tmp/x.py]\n  files:\n" + bad_snippet,
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=match_re):
            load_job(bad_yaml)

    # Also reject symlinks inside an uploaded directory
    sym_dir = REPO / ".workspace" / f"test-symlink-{tmp_path.name}"
    sym_dir.mkdir(parents=True, exist_ok=True)
    try:
        (sym_dir / "ok.py").write_text("print(1)\n", encoding="utf-8")
        (sym_dir / "leak.json").symlink_to(REPO / "tasks" / "harborpoint-oct2026-sla-credit-handoff" / SPEC_DIR / "task.json")
        sym_yaml = tmp_path / "sym.yaml"
        sym_yaml.write_text(
            f"dataset: tasks\nroles: configs/roles/offline-smoke.json\nworker:\n"
            f"  harness: command\n  command: [python3, /tmp/w/ok.py]\n"
            f"  files:\n    - source: {sym_dir.relative_to(REPO).as_posix()}\n      target: /tmp/w\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="symlink"):
            load_job(sym_yaml)
    finally:
        shutil.rmtree(sym_dir, ignore_errors=True)

    # Verify two-turn invocation of examples/custom_worker/agent.py over HarborWorker + workspace
    ws = LocalWorkspace(tmp_path / "ws")
    hw = HarborWorker(ws, [sys.executable, "-B", str(REPO / "examples" / "custom_worker" / "agent.py")], timeout=30)
    r1 = hw.run(
        Call(
            role="worker",
            prompt="Start stage 1",
            instructions="Rules",
            workspace=ws.root,
            session_id=None,
            history=[],
            attachments=[],
            log_dir=tmp_path / "logs" / "t1",
            run_id="run42",
            call_id="s1-01-worker",
            stage_id="s1",
            turn=1,
        )
    )
    assert r1.session_id == "custom-session-run42"
    assert (ws.root / "custom_worker_handoff_log.txt").is_file()

    r2 = hw.run(
        Call(
            role="worker",
            prompt="Continue in stage 2",
            instructions="Rules",
            workspace=ws.root,
            session_id=r1.session_id,
            history=[{"role": "user", "content": "Start stage 1"}, {"role": "assistant", "content": r1.text}],
            attachments=[],
            log_dir=tmp_path / "logs" / "t2",
            run_id="run42",
            call_id="s2-01-worker",
            stage_id="s2",
            turn=1,
        )
    )
    assert r2.session_id == r1.session_id
    assert "Resumed custom-session-run42 on call 2" in r2.text
    assert "Turn 1 initialized by custom-session-run42" in r2.text


def test_role_credential_preflight_and_worker_only_container_env(tmp_path, monkeypatch):
    codex_job = load_job(REPO / "configs" / "job.yaml")
    claude_job = load_job(REPO / "configs" / "claude-code.yaml")
    custom_job = load_job(REPO / "configs" / "custom-worker.yaml")
    pi_roles = json.loads((REPO / "configs" / "roles" / "pi-openai.json").read_text(encoding="utf-8"))
    offline_roles = json.loads((REPO / "configs" / "roles" / "offline-smoke.json").read_text(encoding="utf-8"))

    # 1. Default Codex config only needs OPENAI_API_KEY
    assert missing_credentials(codex_job, pi_roles, env={"OPENAI_API_KEY": "placeholder-openai"}) == []

    # 2. Default Claude config with only ANTHROPIC_API_KEY flags all host OpenAI roles upfront
    miss_claude = missing_credentials(claude_job, pi_roles, env={"ANTHROPIC_API_KEY": "placeholder-anthropic"})
    assert miss_claude == [
        "user: missing OPENAI_API_KEY",
        "reviewer: missing OPENAI_API_KEY",
        "progress_reviewer: missing OPENAI_API_KEY",
    ]

    # 3. key_file satisfies host role credentials even when env var is unset
    kf = tmp_path / "openai.key"
    kf.write_text("file-based-key\n", encoding="utf-8")
    roles_with_file = {
        k: ({**v, "key_file": str(kf)} if isinstance(v, dict) and k in {"user", "reviewer", "progress_reviewer"} else v)
        for k, v in pi_roles.items()
    }
    assert missing_credentials(claude_job, roles_with_file, env={"ANTHROPIC_API_KEY": "placeholder-anthropic"}) == []

    # 4. Offline custom-worker requires zero credentials; key_file without key_env on Worker is rejected
    assert missing_credentials(custom_job, offline_roles, env={}) == []
    keyfile_only_yaml = tmp_path / "keyfile_only.yaml"
    keyfile_only_yaml.write_text(
        f"dataset: tasks\nroles: configs/roles/offline-smoke.json\nworker:\n"
        f"  harness: command\n  command: [python3, /tmp/x.py]\n  key_file: {kf}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="worker.key_env"):
        load_job(keyfile_only_yaml)

    # 5. When both keys are set on host, Claude HarborWorker forwards ONLY ANTHROPIC_API_KEY and config files never contain secret values
    monkeypatch.setenv("OPENAI_API_KEY", "secret-host-openai-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-worker-anthropic-value")
    job_json = write(REPO / "configs" / "claude-code.yaml", out_root=tmp_path / "compiled")
    compiled_text = job_json.read_text(encoding="utf-8") + (job_json.parent / "agent-config.json").read_text(encoding="utf-8")
    assert "secret-host-openai-value" not in compiled_text
    assert "secret-worker-anthropic-value" not in compiled_text

    agent_cfg = json.loads((job_json.parent / "agent-config.json").read_text(encoding="utf-8"))
    captured = {}

    class DummyWorkspace:
        root = Path("/workspace")

        def execute(self, argv, *, stdin, timeout, log_dir, env=None):
            captured["env"] = env
            return json.dumps({"text": "ok", "session_id": "s1"})

    hw = HarborWorker(DummyWorkspace(), **agent_cfg["worker"])
    hw.run(
        Call(
            role="worker",
            prompt="hi",
            instructions="",
            workspace=Path("/workspace"),
            session_id=None,
            history=[],
            attachments=[],
            log_dir=tmp_path / "wlog",
            run_id="r1",
            call_id="c1",
        )
    )
    assert captured["env"] == {"ANTHROPIC_API_KEY": "secret-worker-anthropic-value"}
    assert "OPENAI_API_KEY" not in captured["env"]


def test_check_scoring_requires_design_json_and_rollout_scores_frozen_design(tmp_path):
    task_dir = tmp_path / "missing-design-task"
    (task_dir / SPEC_DIR / "verify").mkdir(parents=True)
    task = {"id": "missing-design-task", "stages": [{"id": "s1", "verifier_checks": [{"id": "c1"}], "criteria": []}]}
    problems = check_scoring(task_dir, task)
    assert any("Missing spec/verify/design.json" in p for p in problems)

    # Historical rollout with its own frozen rollout/design.json scores cleanly even if task['id'] matches
    # a newer task in tasks/ whose spec/verify/design.json has evolved check IDs.
    trial_dir = tmp_path / "trial"
    rollout = trial_dir / "rollout"
    rollout.mkdir(parents=True)
    hist_task = {
        "id": "harborpoint-oct2026-sla-credit-handoff",
        "root": "<host>/bench-to-work/tasks/harborpoint-oct2026-sla-credit-handoff/spec",
        "stages": [
            {
                "id": "s1",
                "verifier_checks": [{"id": "prior_chk_core"}],
                "criteria": [{"id": "prior_crit_minor"}],
            }
        ],
    }
    hist_design = {
        "scoring": {"rule": "core_minor", "minor_weight": 0.2},
        "stages": [
            {
                "id": "s1",
                "scored": True,
                "items": [
                    {"id": "prior_chk_core", "role": "core"},
                    {"id": "prior_crit_minor", "role": "minor"},
                ],
            }
        ],
    }
    hist_report = {
        "run_id": "hist-001",
        "status": "completed",
        "stages": [
            {
                "id": "s1",
                "turns": [
                    {
                        "evaluation": [
                            {"id": "prior_chk_core", "passed": True},
                            {"id": "prior_crit_minor", "passed": False},
                        ]
                    }
                ],
            }
        ],
    }
    (rollout / "task.json").write_text(json.dumps(hist_task), encoding="utf-8")
    (rollout / "design.json").write_text(json.dumps(hist_design), encoding="utf-8")
    (rollout / "report.json").write_text(json.dumps(hist_report), encoding="utf-8")
    (rollout / "harbor.json").write_text(json.dumps({"run_id": "hist-001"}), encoding="utf-8")

    rewards = score_rollout(rollout)
    assert rewards["reward"] == pytest.approx(0.8)
    assert rewards["task_passed"] == 1.0
    assert rewards["items"] == pytest.approx(0.5)

    from types import SimpleNamespace
    from multi_round_harbor.verifier import ReportVerifier

    verifier = object.__new__(ReportVerifier)
    verifier.reward_function = stage_scores
    verifier.trial_paths = SimpleNamespace(trial_dir=trial_dir)
    vres = asyncio.run(verifier.verify())
    assert vres.rewards == rewards

    # Reject non-terminal / error rollouts and missing frozen design.json in score_rollout()
    err_rollout = tmp_path / "err_rollout"
    shutil.copytree(rollout, err_rollout)
    (err_rollout / "report.json").write_text(
        json.dumps({**hist_report, "status": "error"}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="not scorable"):
        score_rollout(err_rollout)

    no_design_trial = tmp_path / "no_design_trial"
    shutil.copytree(trial_dir, no_design_trial)
    (no_design_trial / "rollout" / "design.json").unlink()
    with pytest.raises(FileNotFoundError, match="design.json"):
        score_rollout(no_design_trial / "rollout")

    verifier_missing = object.__new__(ReportVerifier)
    verifier_missing.reward_function = stage_scores
    verifier_missing.trial_paths = SimpleNamespace(trial_dir=no_design_trial)
    with pytest.raises(FileNotFoundError, match="design.json"):
        asyncio.run(verifier_missing.verify())

    bad_rule_trial = tmp_path / "bad_rule_trial"
    shutil.copytree(trial_dir, bad_rule_trial)
    (bad_rule_trial / "rollout" / "design.json").write_text(
        json.dumps({"scoring": {"rule": "unsupported_flat_rule"}, "stages": []}),
        encoding="utf-8",
    )
    verifier_bad_rule = object.__new__(ReportVerifier)
    verifier_bad_rule.reward_function = stage_scores
    verifier_bad_rule.trial_paths = SimpleNamespace(trial_dir=bad_rule_trial)
    with pytest.raises(ValueError, match="core_minor"):
        asyncio.run(verifier_bad_rule.verify())


def test_task_agent_setup_run_and_verifier_end_to_end(tmp_path):
    """Verify TaskAgent.setup -> TaskAgent.run -> ReportVerifier -> score_rollout without rollout/ collision."""
    import os
    from types import SimpleNamespace
    from harbor.models.trial.paths import TrialPaths
    from multi_round_harbor.verifier import ReportVerifier

    trial_dir = tmp_path / "trial"
    logs_dir = trial_dir / "agent"
    logs_dir.mkdir(parents=True)

    # Create a tiny 1-stage task with design.json so setup+run+verifier execute fast and offline
    mini_task_dir = tmp_path / "mini_task"
    (mini_task_dir / SPEC_DIR / "verify").mkdir(parents=True)
    mini_spec = {
        "id": "mini-harbor-e2e",
        "title": "Mini Harbor E2E",
        "persona": "User",
        "background": "Test background",
        "stages": [
            {
                "id": "s1",
                "title": "Stage 1",
                "max_turns": 2,
                "retry_limit": 0,
                "checkpoint": "Submit greeting",
                "needs_workspace": False,
                "initial": {"id": "start", "message": {"type": "fixed", "text": "Say hello."}},
                "verifier_checks": [],
                "criteria": [{"id": "c1", "description": "Greeted cleanly."}],
            }
        ],
    }
    mini_design = {
        "scoring": {"rule": "core_minor", "minor_weight": 0.2},
        "stages": [{"id": "s1", "scored": True, "items": [{"id": "c1", "role": "core"}]}],
    }
    (mini_task_dir / SPEC_DIR / "task.json").write_text(json.dumps(mini_spec), encoding="utf-8")
    (mini_task_dir / SPEC_DIR / "verify" / "design.json").write_text(json.dumps(mini_design), encoding="utf-8")
    (trial_dir / "config.json").write_text(
        json.dumps({"task": {"path": str(mini_task_dir)}}), encoding="utf-8"
    )

    job_json = write(REPO / "configs" / "custom-worker.yaml", out_root=tmp_path / "job_out")
    agent_cfg_path = job_json.parent / "agent-config.json"

    class FakeLocalDockerEnv:
        def __init__(self, root: Path):
            self.root = root
            (self.root / "tmp").mkdir(parents=True, exist_ok=True)

        def _map(self, s: str) -> str:
            return (
                s.replace("/workspace", str(self.root / "workspace"))
                .replace("/tmp/custom-worker", str(self.root / "custom-worker"))
                .replace("/tmp/harbor-", str(self.root / "tmp" / "harbor-"))
            )

        async def exec(self, command: str, timeout_sec: int = 30, env: dict | None = None, cwd: str | None = None):
            mapped = self._map(command)
            mapped_cwd = self._map(cwd) if cwd else None
            p = subprocess.run(
                ["bash", "-c", mapped],
                cwd=mapped_cwd,
                env={**os.environ, **(env or {})},
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            return SimpleNamespace(return_code=p.returncode, stdout=p.stdout, stderr=p.stderr)

        async def upload_file(self, source: Path, target: str):
            dst = Path(self._map(target))
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dst)

        async def download_file(self, source: str, target: Path):
            src = Path(self._map(source))
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)

    fake_env = FakeLocalDockerEnv(tmp_path / "container_fs")
    agent = TaskAgent(
        logs_dir=logs_dir,
        config_path=str(agent_cfg_path),
        project=str(REPO.resolve()),
    )
    agent.config["workspace"]["root"] = str((tmp_path / "container_fs" / "ws").resolve())
    ctx = SimpleNamespace(metadata={})
    asyncio.run(agent.setup(fake_env))
    assert not agent.run_dir.exists(), "setup() must leave rollout/ creation exclusively to Runner.run()"
    asyncio.run(agent.run("Run task", fake_env, ctx))

    assert (agent.run_dir / "report.json").is_file()
    assert (agent.run_dir / "design.json").is_file()
    assert (agent.run_dir / "harbor.json").is_file()

    verifier = ReportVerifier(
        task=None,
        trial_paths=TrialPaths(trial_dir),
        environment=None,
        reward_function="bench_to_work.rewards:stage_scores",
    )
    vres = asyncio.run(verifier.verify())
    assert vres.rewards == score_rollout(agent.run_dir)
    assert vres.rewards["reward"] == 1.0
    assert vres.rewards["task_passed"] == 1.0


def test_checked_review_propagates_recheck_failure_and_persistent_contradictions(tmp_path):
    contradictory = json.dumps(
        {"reason": "r", "criteria": [{"id": "c1", "passed": True, "evidence": "Failed check. Verdict: FAIL."}]}
    )

    class FailingRecheckAgent:
        def __init__(self):
            self.calls = 0

        def run(self, call):
            self.calls += 1
            if self.calls == 1:
                return Reply(text=contradictory)
            raise ExternalExecutionError("recheck subprocess crashed")

    log1 = tmp_path / "rev1"
    log1.mkdir()
    call1 = Call(role="reviewer", prompt="Evaluate", workspace=None, log_dir=log1, call_id="c1", run_id="r1")
    with pytest.raises(ExternalExecutionError, match="recheck subprocess crashed"):
        CheckedReview(FailingRecheckAgent(), rechecks=1, retries=0).run(call1)
    assert (log1 / "recheck-1" / "failed.txt").is_file()

    class StubbornContradictoryAgent:
        def run(self, call):
            return Reply(text=contradictory)

    log2 = tmp_path / "rev2"
    log2.mkdir()
    call2 = Call(role="reviewer", prompt="Evaluate", workspace=None, log_dir=log2, call_id="c2", run_id="r2")
    with pytest.raises(AdapterError, match="contradictory verdict/evidence"):
        CheckedReview(StubbornContradictoryAgent(), rechecks=1, retries=0).run(call2)


def test_check_and_smoke_fail_fast_on_empty_dataset(tmp_path):
    empty = tmp_path / "empty_tasks"
    empty.mkdir()
    assert check_main(["--dataset", str(empty)]) == 2
    assert smoke_main(["--dataset", str(empty)]) == 2


def test_score_rollout_applies_validated_audit_overrides(tmp_path):
    rollout = tmp_path / "rollout"
    rollout.mkdir()
    task = {
        "id": "demo",
        "stages": [
            {
                "id": "s1",
                "verifier_checks": [{"id": "chk1", "hard_gate": False}],
                "criteria": [{"id": "cri1"}],
            }
        ],
    }
    design = {
        "scoring": {"rule": "core_minor", "minor_weight": 0.2},
        "stages": [{"id": "s1", "items": [{"id": "chk1", "role": "core"}, {"id": "cri1", "role": "core"}]}],
    }
    report = {
        "status": "completed",
        "stages": [{"id": "s1", "turns": [{"evaluation": [{"id": "chk1", "passed": True}, {"id": "cri1", "passed": True}]}]}],
    }
    (rollout / "task.json").write_text(json.dumps(task), encoding="utf-8")
    (rollout / "design.json").write_text(json.dumps(design), encoding="utf-8")
    (rollout / "report.json").write_text(json.dumps(report), encoding="utf-8")
    assert score_rollout(rollout)["reward"] == 1.0

    (rollout / "audit_overrides.json").write_text(
        json.dumps([{"stage_id": "s1", "id": "cri1", "original_passed": True, "passed": False, "reason": "Audited"}]),
        encoding="utf-8",
    )
    calibrated = score_rollout(rollout)
    assert calibrated["reward"] == 0.0
    assert calibrated["items"] == 0.5

    (rollout / "audit_overrides.json").write_text(
        json.dumps([{"stage_id": "s1", "id": "cri1", "original_passed": False, "passed": False, "reason": "Wrong baseline"}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="expected original_passed=False"):
        score_rollout(rollout)

    (rollout / "audit_overrides.json").write_text(
        json.dumps([{"stage_id": "s1", "id": "cri1", "passed": False, "reason": "Missing original_passed"}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="original_passed"):
        score_rollout(rollout)

    (rollout / "audit_overrides.json").write_text(
        json.dumps([{"stage_id": "s1", "id": "unknown", "original_passed": True, "passed": False, "reason": "Bad id"}]),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown stage/item override"):
        score_rollout(rollout)


def test_smoke_allocates_unique_run_dirs_and_rejects_existing_output(tmp_path, monkeypatch):
    import concurrent.futures
    import bench_to_work.smoke as smoke_mod

    task_dir = TASK_DIRS["harborpoint-oct2026-sla-credit-handoff"]
    existing = tmp_path / "already_exists"
    existing.mkdir()
    with pytest.raises(FileExistsError, match="already exists"):
        smoke(task_dir, existing)

    monkeypatch.setattr(smoke_mod, "REPO", tmp_path)

    def run_once():
        return smoke_main(["--dataset", str(DATASET), "harborpoint-oct2026-sla-credit-handoff"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(run_once)
        f2 = pool.submit(run_once)
        assert f1.result() == 0
        assert f2.result() == 0

    reports = list((tmp_path / ".workspace" / "smoke").glob("*/report.json"))
    assert len(reports) == 2
    assert len({p.parent.resolve() for p in reports}) == 2


def test_check_public_rejects_private_paths_and_leaked_check_ids(tmp_path):
    task_dir = tmp_path / "sample_task"
    shutil.copytree(TASK_DIRS["harborpoint-oct2026-sla-credit-handoff"], task_dir)
    task = json.loads((task_dir / SPEC_DIR / "task.json").read_text(encoding="utf-8"))
    assert check_public(task_dir, task) == []

    # 1. Any file under environment/public/verify/ exposes controller-private verification assets.
    leaked_verify = task_dir / "environment" / "public" / "verify" / "helper.py"
    spec_verify = task_dir / SPEC_DIR / "verify" / "helper.py"
    leaked_verify.parent.mkdir(parents=True, exist_ok=True)
    leaked_verify.write_text("x = 1\n", encoding="utf-8")
    spec_verify.write_text("x = 1\n", encoding="utf-8")
    issues_verify = check_public(task_dir, task)
    assert any("exposes controller-private directory 'verify/'" in msg for msg in issues_verify)
    shutil.rmtree(task_dir / "environment" / "public" / "verify")
    spec_verify.unlink()

    # 2. Unreferenced public files on a hookless task must also be rejected, and a hook script
    # mentioning a private check or criterion ID must be rejected even when referenced by a hook.
    first_id = task["stages"][0]["verifier_checks"][0]["id"]
    hook_script = f"# Public preview helper mentioning {first_id}\nprint('ok')\n"
    (task_dir / SPEC_DIR / "preview_hook.py").write_text(hook_script, encoding="utf-8")
    (task_dir / "environment" / "public" / "preview_hook.py").write_text(hook_script, encoding="utf-8")
    issues_unref = check_public(task_dir, task)
    assert any("declares no '{task}/...' hooks" in msg for msg in issues_unref)

    task_with_hook = {
        **task,
        "hooks": [{"id": "preview", "event": "after_worker", "command": ["{python}", "{task}/preview_hook.py"]}],
    }
    issues_id = check_public(task_dir, task_with_hook)
    assert any("leaks private check/criterion IDs" in msg and first_id in msg for msg in issues_id)

    # 3. Empty verify/ directory or controller-private spec file (verifier.py) must be rejected.
    (task_dir / "environment" / "public" / "preview_hook.py").unlink()
    (task_dir / SPEC_DIR / "preview_hook.py").unlink()
    (task_dir / "environment" / "public" / "verify").mkdir()
    assert any("exposes controller-private directory 'verify/'" in msg for msg in check_public(task_dir, task))
    (task_dir / "environment" / "public" / "verify").rmdir()

    shutil.copy2(task_dir / SPEC_DIR / "verifier.py", task_dir / "environment" / "public" / "verifier.py")
    assert any("exposes controller-private spec file 'verifier.py'" in msg for msg in check_public(task_dir, task_with_hook))


def test_bilingual_transient_and_contradiction_patterns():
    assert TRANSIENT.search("网关异常：读取超时，请稍后重试")
    assert TRANSIENT.search("上游服务连接失败")
    assert not TRANSIENT.search("JSON schema validation failed")

    bad_fail = contradictions(
        json.dumps(
            {"criteria": [{"id": "zh_fail", "passed": True, "evidence": "核对交付工作簿发现关键公式缺失，判定：不通过。"}]},
            ensure_ascii=False,
        )
    )
    assert [item["id"] for item in bad_fail] == ["zh_fail"]

    bad_pass = contradictions(
        json.dumps(
            {"criteria": [{"id": "zh_pass", "passed": False, "evidence": "所有必填字段与公式核对无误，本项通过。"}]},
            ensure_ascii=False,
        )
    )
    assert [item["id"] for item in bad_pass] == ["zh_pass"]


def test_shipped_pi_openai_roles_match_official_protocol_and_preflight_guards(tmp_path, monkeypatch):
    import httpx
    from openai import AsyncOpenAI
    from bench_to_work.job import agent_config, load as load_job, validate_role_protocols
    from multi_round.agents import make_agent, openai_model

    roles = json.loads((REPO / "configs" / "roles" / "pi-openai.json").read_text(encoding="utf-8"))
    assert validate_role_protocols(roles) == []
    assert roles["user"]["api"] == "openai-responses"
    assert roles["reviewer"]["api"] == "openai-responses"
    assert roles["progress_reviewer"]["temperature"] is None
    assert roles["progress_reviewer"]["max_tokens_field"] == "max_completion_tokens"

    # Verify shipped progress_reviewer request omits temperature and sends max_completion_tokens
    monkeypatch.setenv("OPENAI_API_KEY", "local-test-key")
    seen_bodies = []

    def mock_client(**options):
        def handler(req: httpx.Request) -> httpx.Response:
            seen_bodies.append(json.loads(req.content.decode("utf-8")))
            payload = {
                "id": "c1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "gpt-6.2-sol",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "content": json.dumps(
                                {"reason": "ok", "decision": "review_submission", "continuation_id": None}
                            )
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
            chunk = f"data: {json.dumps(payload)}\n\ndata: [DONE]\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=chunk.encode("utf-8"))

        return AsyncOpenAI(**options, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(openai_model, "AsyncOpenAI", mock_client)
    judge = make_agent(roles["progress_reviewer"], REPO)
    reply = judge.run(Call(role="reviewer", prompt="Check turn", workspace=None, log_dir=tmp_path / "judge_log"))
    assert json.loads(reply.text)["decision"] == "review_submission"
    assert len(seen_bodies) == 1
    assert "temperature" not in seen_bodies[0]
    assert "max_tokens" not in seen_bodies[0]
    assert seen_bodies[0]["max_completion_tokens"] == 32768

    # Preflight rejects verified-incompatible Terra/Sol official OpenAI combinations before starting any container
    job = load_job(REPO / "configs" / "job.yaml")
    bad_terra = {**roles, "user": {**roles["user"], "api": "openai-completions"}}
    with pytest.raises(ValueError, match="openai-responses"):
        agent_config(job, bad_terra)

    bad_sol = {**roles, "reviewer": {**roles["reviewer"], "api": "openai-completions"}}
    with pytest.raises(ValueError, match="openai-responses"):
        agent_config(job, bad_sol)

    bad_temp = {**roles, "progress_reviewer": {**roles["progress_reviewer"], "temperature": 0}}
    with pytest.raises(ValueError, match="temperature"):
        agent_config(job, bad_temp)

    bad_tok = {**roles, "progress_reviewer": {**roles["progress_reviewer"], "max_tokens_field": "max_tokens"}}
    with pytest.raises(ValueError, match="max_completion_tokens"):
        agent_config(job, bad_tok)

    # Official Chat Completions tool calls ARE supported for gpt-6-luna with reasoning_effort='none'
    luna_roles = {
        **roles,
        "user": {
            **roles["user"],
            "model": "gpt-6-luna",
            "api": "openai-completions",
            "reasoning": True,
            "thinking_level": "off",
            "thinking_level_map": {"off": "none"},
            "compat": {"supportsReasoningEffort": True, "maxTokensField": "max_completion_tokens"},
        },
    }
    assert validate_role_protocols(luna_roles) == []
    compiled_luna = agent_config(job, luna_roles)
    assert compiled_luna["user"]["model"] == "gpt-6-luna"
    assert compiled_luna["user"]["api"] == "openai-completions"


def test_harbor_smoke_fails_on_nonzero_harbor_exit_and_streams_log_before_timeout(tmp_path, monkeypatch):
    import bench_to_work.smoke as smoke_mod

    task_dir = TASK_DIRS["harborpoint-oct2026-sla-credit-handoff"]
    spec_task_data = json.loads((task_dir / SPEC_DIR / "task.json").read_text(encoding="utf-8"))

    # 1. Trial finishes with reward=1.0, then Harbor exits 1 while writing job summary -> ok(r) MUST be False, trial kept
    def fake_harbor_nonzero(cmd, *, cwd, env, stdout, stderr, timeout):
        out_dir = Path(cmd[cmd.index("--config") + 1]).parent
        trial = out_dir / "jobs" / out_dir.name / "trial-001"
        (trial / "rollout").mkdir(parents=True)
        stdout.write(b"Trial finished; simulating job summary write failure\n")
        stdout.flush()
        stages_rep = [
            {
                "id": s["id"],
                "status": "passed",
                "turns": [{"evaluation": [{"id": c["id"], "passed": True} for c in s["verifier_checks"] + s["criteria"]]}],
            }
            for s in spec_task_data["stages"]
        ]
        (trial / "rollout" / "report.json").write_text(
            json.dumps({"status": "completed", "stages": stages_rep, "hooks": []}), encoding="utf-8"
        )
        (trial / "rollout" / "task.json").write_text(json.dumps(spec_task_data), encoding="utf-8")
        (trial / "result.json").write_text(
            json.dumps({"verifier_result": {"rewards": {"reward": 1.0, "task_passed": 1.0}}, "exception_info": None}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(smoke_mod.subprocess, "run", fake_harbor_nonzero)
    out_nz = tmp_path / "smoke_nz"
    r_nz = smoke_mod.harbor_smoke(task_dir, out_nz)
    assert r_nz["harbor_returncode"] == 1
    assert not smoke_mod.ok(r_nz)
    assert Path(r_nz["run_dir"], "rollout", "report.json").is_file()
    assert "job summary write failure" in (out_nz / "harbor.log").read_text(encoding="utf-8")

    # 2. Harbor times out -> partial harbor.log is already flushed on disk before TimeoutExpired propagates
    def fake_harbor_timeout(cmd, *, cwd, env, stdout, stderr, timeout):
        stdout.write(b"Partial container build output before timeout\n")
        stdout.flush()
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(smoke_mod.subprocess, "run", fake_harbor_timeout)
    out_to = tmp_path / "smoke_timeout"
    with pytest.raises(subprocess.TimeoutExpired):
        smoke_mod.harbor_smoke(task_dir, out_to)
    assert "Partial container build output before timeout" in (out_to / "harbor.log").read_text(encoding="utf-8")
