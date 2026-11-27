"""Harbor multi-task entry point: resolves <task_dir>/spec/task.json per trial from <trial>/config.json."""
from __future__ import annotations

import json
from pathlib import Path

from multi_round_harbor.agent import MainAgent

from . import SPEC_DIR


def spec_task(trial_dir: Path) -> Path:
    """Resolve the controller-side task specification (<task_dir>/spec/task.json) from Harbor's <trial>/config.json."""
    config = json.loads((trial_dir / "config.json").read_text(encoding="utf-8"))
    path = (config.get("task") or {}).get("path")
    if not path:
        raise ValueError(f"Missing local task path in {trial_dir / 'config.json'}")
    task = Path(path).resolve() / SPEC_DIR / "task.json"
    if not task.is_file():
        raise FileNotFoundError(f"Task directory {Path(path).resolve()} is missing {SPEC_DIR}/task.json")
    return task


class TaskAgent(MainAgent):
    def __init__(self, *args, config_path: str, project: str, task_path: str | None = None, **kwargs):
        super().__init__(*args, task_path=task_path or ".", config_path=config_path, project=project, **kwargs)
        self.task_fixed = task_path is not None

    @staticmethod
    def name() -> str:
        return "bench-to-work"

    async def setup(self, environment):
        if not self.task_fixed:
            self.task_path = spec_task(self.logs_dir.parent)
        await super().setup(environment)
