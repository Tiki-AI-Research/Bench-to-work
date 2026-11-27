"""Harbor task entry point: wire roles and remote workspace, then delegate multi-round scheduling to Runner."""

import asyncio
import json
import shlex
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

from harbor.agents.base import BaseAgent

from multi_round.agents import make_agent
from multi_round.agents.lifecycle import close_agent, finish_agent_cleanup
from multi_round.models import ExternalExecutionError, load_task
from multi_round.runner import Runner
from multi_round.storage import write_json

from .bridge import EnvironmentBridge, GuardedAgent
from .worker import HarborWorker
from .workspace import HarborWorkspace


class MainAgent(BaseAgent):
    def __init__(self, *args, task_path: str, config_path: str, project: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.project = Path(project).resolve()
        self.task_path = Path(task_path).resolve()
        self.config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        # Harbor mounts agent/ into the Worker container; hidden rubrics and trusted reports stay outside under rollout/.
        self.run_dir = self.logs_dir.parent / "rollout"

    @staticmethod
    def name():
        return "multi-round-main"

    def version(self):
        return "0.1.0"

    async def setup(self, environment):
        options = self.config.get("workspace", {})
        root = options.get("root", "/workspace")
        result = await environment.exec(
            f"mkdir -p {shlex.quote(root)} && test -w {shlex.quote(root)} && command -v timeout",
            timeout_sec=30,
        )
        if result.return_code != 0:
            raise ExternalExecutionError(
                "Harbor Worker image requires GNU timeout and a writable workspace directory"
            )
        # Upload only explicitly declared public files; never upload the full task directory or future stage materials.
        for item in self.config.get("public_files", []):
            source = self.project / item["source"]
            target = item["target"]
            result = await environment.exec(
                "mkdir -p " + shlex.quote(str(Path(target).parent)), timeout_sec=30
            )
            if result.return_code != 0:
                raise ExternalExecutionError(
                    f"Failed to prepare public file directory: {target}"
                )
            await environment.upload_file(source, target)

    async def run(self, instruction, environment, context):
        bridge = EnvironmentBridge()
        workspace = HarborWorkspace(
            environment, bridge, **self.config.get("workspace", {})
        )
        context.metadata = {"report_path": str(self.run_dir / "report.json")}

        def run():
            task = load_task(self.task_path)
            design_file = self.task_path.parent / "verify" / "design.json"
            frozen_design = (
                json.loads(design_file.read_text(encoding="utf-8"))
                if design_file.is_file()
                else None
            )
            task = replace(
                task,
                verifier=[
                    arg.replace("{project}", str(self.project)) for arg in task.verifier
                ],
            )
            report, cleanup_errors = None, []
            try:
                with ExitStack() as stack:
                    agents = {}
                    for role in ("user", "reviewer", "progress_reviewer"):
                        if role == "progress_reviewer" and role not in self.config:
                            continue
                        agent = make_agent(self.config[role], self.project)
                        if close := getattr(agent, "close", None):
                            stack.callback(close_agent, close, role, cleanup_errors)
                        agents[role] = GuardedAgent(agent, bridge)
                    agents["worker"] = GuardedAgent(
                        HarborWorker(workspace, **self.config["worker"]), bridge
                    )
                    report = Runner(
                        **agents,
                        reviewer_protocol_retry_limit=self.config.get(
                            "reviewer_protocol_retry_limit", 1
                        ),
                        verifier_timeout=self.config.get("verifier_timeout", 60),
                        progress=self.logger.info,
                        public_language=self.config.get("public_language"),
                    ).run(
                        task,
                        self.run_dir,
                        mode="harbor",
                        run_config=self.config,
                        frozen_design=frozen_design,
                        workspace=workspace,
                    )
                    if report["status"] == "error":
                        raise ExternalExecutionError(report["reason"])
                    return report
            finally:
                finish_agent_cleanup(cleanup_errors, report, self.run_dir)

        pending = asyncio.create_task(asyncio.to_thread(run))
        try:
            # Shield Runner so cancellation lets the active bounded call finish cleanup and write report.json.
            await asyncio.shield(pending)
        except asyncio.CancelledError as cancelled:
            bridge.cancelled.set()
            try:
                await pending
            except Exception as exc:
                # Runner already recorded teardown errors; attach note while preserving Harbor's original CancelledError.
                cancelled.add_note(
                    f"Post-cancellation teardown raised: {type(exc).__name__}: {exc}"
                )
            raise
        finally:
            if (self.run_dir / "report.json").exists():
                saved = json.loads(
                    (self.run_dir / "report.json").read_text(encoding="utf-8")
                )
                link = {
                    "trial_id": str(self.context_id),
                    "run_id": saved["run_id"],
                    "status": saved["status"],
                    "worker_workspace": str(workspace.root),
                }
                write_json(self.run_dir / "harbor.json", link)
                context.metadata.update(link)
