"""Read Runner's saved report.json and compute Harbor rewards without re-invoking the Judge."""

import inspect
import json

from harbor.models.verifier.result import VerifierResult
from harbor.verifier.base import BaseVerifier

from multi_round.factories import load_factory


class ReportVerifier(BaseVerifier):
    def __init__(self, *, reward_function: str, **kwargs):
        super().__init__(**kwargs)
        self.reward_function = load_factory(reward_function)

    async def verify(self):
        directory = self.trial_paths.trial_dir / "rollout"
        report = json.loads((directory / "report.json").read_text(encoding="utf-8"))
        link = json.loads((directory / "harbor.json").read_text(encoding="utf-8"))
        if link["run_id"] != report["run_id"] or report["status"] not in {
            "completed",
            "failed",
            "budget_exhausted",
        }:
            raise ValueError("Runner did not produce a scorable completed/terminal run record")
        task = json.loads((directory / "task.json").read_text(encoding="utf-8"))
        params = inspect.signature(self.reward_function).parameters
        if "rollout_dir" in params:
            return VerifierResult(rewards=self.reward_function(report, task, rollout_dir=directory))
        design_path = directory / "design.json"
        if design_path.is_file():
            accepts_design = "design" in params or any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
            if accepts_design:
                design = json.loads(design_path.read_text(encoding="utf-8"))
                return VerifierResult(rewards=self.reward_function(report, task, design=design))
        return VerifierResult(rewards=self.reward_function(report, task))


def final_check_fraction(report: dict, task: dict) -> dict[str, float]:
    """Demo reward: passed checks in each stage's final evaluation divided by total planned checks (untested stages score 0).

    Intended for demonstration only; RL training pipelines should handle retries, per-stage credit, and token alignment explicitly.
    """
    total = sum(len(s["criteria"]) + len(s["verifier_checks"]) for s in task["stages"])
    passed = 0
    for stage in report["stages"]:
        evaluations = [t["evaluation"] for t in stage["turns"] if "evaluation" in t]
        if evaluations:
            passed += sum(c["passed"] for c in evaluations[-1])
    if not total:
        raise ValueError("Task has no evaluation checks; cannot compute demo reward")
    return {"reward": passed / total}
