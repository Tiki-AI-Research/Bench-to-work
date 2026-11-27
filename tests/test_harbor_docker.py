"""Opt-in Harbor + Docker integration checks including sample image builds without LLM calls."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from multi_round_harbor.bridge import EnvironmentBridge
from multi_round_harbor.verifier import ReportVerifier
from multi_round_harbor.workspace import HarborWorkspace
from bench_to_work.job import mirror_compose
from harbor.environments.docker.docker import DockerEnvironment
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from multi_round.models import ExternalExecutionError


pytestmark = pytest.mark.skipif(
    os.getenv("MULTI_ROUND_HARBOR_DOCKER_TEST") != "1",
    reason="Set MULTI_ROUND_HARBOR_DOCKER_TEST=1 to run local Docker checks",
)


def test_excel_example_environment(tmp_path):
    """Build the Harbor sample image and verify non-root workspace and login-shell Excel support."""

    async def probe():
        paths = TrialPaths(tmp_path / "trial")
        paths.mkdir()
        environment = DockerEnvironment(
            environment_dir=Path(__file__).resolve().parent
            / "fixtures/harbor/excel/environment",
            environment_name="multi-round-excel-probe",
            session_id="excel-probe-" + uuid4().hex[:12],
            trial_paths=paths,
            task_env_config=EnvironmentConfig(),
            extra_docker_compose=mirror_compose(tmp_path),
        )
        try:
            await environment.start(force_build=False)
            result = await environment.exec("codex --version")
            assert result.return_code == 0 and "0.154.0" in result.stdout
            result = await environment.exec(
                """bash -lc 'python3 - <<"PY"
import json, os
from pathlib import Path
from openpyxl import Workbook, load_workbook
assert os.getuid() != 0
path = Path("/workspace/probe.xlsx")
book = Workbook()
book.active["A1"] = 7
book.active["A2"] = "=A1*2"
book.save(path)
book.close()
book = load_workbook(path)
assert book.active["A1"].value == 7 and book.active["A2"].value == "=A1*2"
book.close()
assert Path("/opt/worker.py").is_file()
print(json.dumps({"office_roundtrip": True, "uid": os.getuid()}))
PY'"""
            )
            assert result.return_code == 0, result
            assert json.loads(result.stdout)["office_roundtrip"] is True
        finally:
            await environment.stop(delete=True)

    asyncio.run(probe())


def test_real_docker_workspace_and_evidence_boundary(tmp_path):
    async def probe():
        paths = TrialPaths(tmp_path / "trial")
        paths.mkdir()
        private = paths.trial_dir / "rollout"
        private.mkdir()
        (private / "hidden.txt").write_text(uuid4().hex)
        environment_dir = tmp_path / "environment"
        environment_dir.mkdir()
        environment = DockerEnvironment(
            environment_dir=environment_dir,
            environment_name="harbor-workspace-probe",
            session_id="harbor-probe-" + uuid4().hex[:12],
            trial_paths=paths,
            task_env_config=EnvironmentConfig(docker_image="python:3.12-slim-bookworm"),
        )
        try:
            await environment.start(force_build=False)
            await environment.exec("mkdir -p /workspace")
            bridge = EnvironmentBridge()
            workspace = HarborWorkspace(environment, bridge)
            source = tmp_path / "input.txt"
            source.write_text("before")

            def exercise():
                workspace.publish(source, "input.txt")
                # Mutate the container workspace through HarborWorkspace and verify stdout/stderr separation.
                result = workspace.execute(
                    [
                        "python3",
                        "-c",
                        "from pathlib import Path; import sys; Path('input.txt').write_text('after'); print('{}'); print('diagnostic',file=sys.stderr)",
                    ],
                    stdin="",
                    timeout=5,
                    log_dir=tmp_path / "hook",
                )
                assert json.loads(result) == {}
                assert (
                    tmp_path / "hook/stderr.log"
                ).read_text().strip() == "diagnostic"
                workspace.fetch_file("input.txt", tmp_path / "sent.txt")
                assert (tmp_path / "sent.txt").read_text() == "after"
                workspace.collect(["input.txt", "missing"], tmp_path / "first")
                workspace.execute(
                    ["rm", "input.txt"],
                    stdin="",
                    timeout=5,
                    log_dir=tmp_path / "remove",
                )
                assert workspace.collect(["input.txt"], tmp_path / "second") == {}
                assert (tmp_path / "first/input.txt").read_text() == "after"
                with pytest.raises(ExternalExecutionError):
                    workspace.execute(
                        ["sh", "-c", "sleep 1; touch late"],
                        stdin="",
                        timeout=0.1,
                        log_dir=tmp_path / "timeout",
                    )
                with pytest.raises(ExternalExecutionError):
                    workspace.execute(
                        ["cat", str(private / "hidden.txt")],
                        stdin="",
                        timeout=5,
                        log_dir=tmp_path / "hidden",
                    )

            await asyncio.to_thread(exercise)
            await asyncio.sleep(1.1)
            assert (
                await environment.exec("test ! -e /workspace/late")
            ).return_code == 0
            assert (
                await environment.exec("test ! -e /logs/rollout/hidden.txt")
            ).return_code == 0
            # Even if the Worker tampers with shared container logs, final rewards read controller rollout/ only.
            await environment.exec("echo '{\"reward\":1}' > /logs/verifier/reward.json")
            report = {
                "run_id": "probe",
                "status": "failed",
                "stages": [{"turns": [{"evaluation": [{"passed": False}]}]}],
            }
            for name, value in {
                "report": report,
                "harbor": {"run_id": "probe"},
                "task": {"stages": [{"criteria": [1], "verifier_checks": []}]},
            }.items():
                (private / f"{name}.json").write_text(json.dumps(value))
            verifier = ReportVerifier(
                task=None,
                trial_paths=paths,
                environment=environment,
                reward_function="multi_round_harbor.verifier:final_check_fraction",
            )
            assert (await verifier.verify()).rewards == {"reward": 0}
            (tmp_path / "probe.json").write_text(
                json.dumps(
                    {
                        "hook_and_sent_material": "after",
                        "deleted_submission_absent": True,
                        "timeout_child_stopped": True,
                        "hidden_record_inaccessible": True,
                        "forged_shared_reward_ignored": True,
                    },
                    indent=2,
                )
            )
        finally:
            await environment.stop(delete=True)

    asyncio.run(probe())
