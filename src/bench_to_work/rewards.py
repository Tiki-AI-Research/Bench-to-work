"""Reward calculation from trusted controller-side rollout/report.json.

Supports two-tier outcome scoring (`spec/verify/design.json` with `rule = "core_minor"`):
- Each check/criterion in a stage is tagged as `core` (must-pass delivery & core business judgment)
  or `minor` (non-fatal supplementary/routine details).
- If any `core` item in a scored stage fails, `stage_score = 0.0`.
- When all `core` items in the stage pass:
  - If the stage has no `minor` items, `stage_score = 1.0`.
  - Otherwise, failing `minor` items deducts proportionally up to `minor_weight` (default `0.2`):
    `stage_score = (1 - minor_weight) + minor_weight * minor_pass_rate`.
- `stages_passed` and `task_passed` (`Task Pass@1`) evaluate `core` items ONLY: non-fatal `minor`
  omissions slightly reduce `reward` without flipping stage/task pass status to 0.
- Stages marked `scored: false` (setup stages) are verified normally but excluded from scored metrics.
- Missing or non-`core_minor` `design.json` raises an explicit error rather than silently falling back to item-average scoring.
- Optional `rollout/audit_overrides.json` records human-audited item calibrations without overwriting raw `report.json` logs.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import REPO, SPEC_DIR

RULE = "core_minor"
ROLES = ("core", "minor")
DEFAULT_MINOR_WEIGHT = 0.2
SCORABLE_STATUSES = ("completed", "failed", "budget_exhausted")


def last_evaluations(report: dict) -> dict[str, list[dict]]:
    """Return the final formal evaluation item list for each evaluated stage."""
    out = {}
    for stage in report["stages"]:
        evaluations = [t["evaluation"] for t in stage["turns"] if "evaluation" in t]
        if evaluations:
            out[stage["id"]] = evaluations[-1]
    return out


def load_design(task: dict) -> dict | None:
    """Locate verify/design.json from recorded task root or `tasks/**/<task_id>/spec/verify/design.json`."""
    paths = [Path(task["root"]) / "verify" / "design.json", Path(task["root"]) / "design.json"] if task.get("root") else []
    if task.get("id"):
        paths += sorted((REPO / "tasks").glob(f"**/{task['id']}/{SPEC_DIR}/verify/design.json"))
        paths += sorted((REPO / "tasks").glob("**/verify/design.json"))
    for p in paths:
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8"))
            if (len(p.parents) > 2 and p.parents[2].name == task.get("id")) or (
                task.get("root") and p in {Path(task["root"]) / "verify" / "design.json", Path(task["root"]) / "design.json"}
            ):
                return data
            sibling_task = p.parents[1] / "task.json"
            if sibling_task.is_file():
                if json.loads(sibling_task.read_text(encoding="utf-8")).get("id") == task.get("id"):
                    return data
    return None


def tagged(design: dict | None) -> bool:
    return bool(design) and (design.get("scoring") or {}).get("rule") == RULE


def stage_roles(stage: dict, design: dict) -> tuple[list[str], list[str]]:
    stage_spec = next((s for s in design["stages"] if s["id"] == stage["id"]), {"items": []})
    tags = {it["id"]: it for it in stage_spec.get("items", [])}
    core_ids: list[str] = []
    minor_ids: list[str] = []
    for c in stage["verifier_checks"] + stage["criteria"]:
        it = tags.get(c["id"])
        if not it or it.get("role") not in ROLES:
            raise ValueError(f"{stage['id']}/{c['id']} is missing a valid role in design.json ({ROLES})")
        if "group" in it:
            raise ValueError(f"{stage['id']}/{c['id']} contains unsupported 'group' field in design.json; specify 'role' ({ROLES})")
        if it["role"] == "core":
            core_ids.append(c["id"])
        else:
            minor_ids.append(c["id"])
    return core_ids, minor_ids


def scored(stage: dict, design: dict) -> bool:
    d = next((s for s in design["stages"] if s["id"] == stage["id"]), {})
    return bool(stage["verifier_checks"] or stage["criteria"]) and d.get("scored", True) is not False


def core_minor_scores(last: dict[str, list[dict]], task: dict, design: dict) -> dict[str, float]:
    minor_weight = float((design.get("scoring") or {}).get("minor_weight", DEFAULT_MINOR_WEIGHT))
    if not (0.0 <= minor_weight < 1.0):
        raise ValueError(f"minor_weight must be in [0.0, 1.0), got {minor_weight}")
    rounds: list[tuple[float, bool]] = []
    for stage in task["stages"]:
        if not scored(stage, design):
            continue
        core_ids, minor_ids = stage_roles(stage, design)
        if not core_ids:
            raise ValueError(f"Scored stage {stage['id']} has no `core` items in design.json")
        ok = {c["id"] for c in last.get(stage["id"], []) if c["passed"]}
        core_ok = set(core_ids) <= ok
        if not core_ok:
            score = 0.0
        elif not minor_ids:
            score = 1.0
        else:
            minor_rate = len(set(minor_ids) & ok) / len(minor_ids)
            score = (1.0 - minor_weight) + minor_weight * minor_rate
        rounds.append((score, core_ok))
    if not rounds:
        raise ValueError("Tagged task has no scored stages")
    return {
        "reward": sum(s for s, _ in rounds) / len(rounds),
        "task_passed": 1.0 if all(c_ok for _, c_ok in rounds) else 0.0,
        "stages_passed": sum(1 for _, c_ok in rounds if c_ok) / len(rounds),
    }


def _resolve_validated_design(
    task: dict,
    design: dict | None,
    rollout_dir: Path | None,
) -> dict:
    if design is None:
        if rollout_dir is not None:
            design_path = Path(rollout_dir) / "design.json"
            if not design_path.is_file():
                raise FileNotFoundError(
                    f"Rollout directory is missing frozen design.json: {design_path} "
                    "(or pass design=... explicitly)"
                )
            design = json.loads(design_path.read_text(encoding="utf-8"))
        else:
            design = load_design(task)
            if design is None:
                raise FileNotFoundError(
                    f"Task {task.get('id')!r} is missing required {SPEC_DIR}/verify/design.json"
                )
    if not tagged(design):
        raise ValueError(f"Scoring design must specify scoring.rule == {RULE!r}")
    return design


def _load_audit_overrides(
    rollout_dir: Path | None,
    planned: dict[str, tuple[set[str], set[str]]],
) -> dict[tuple[str, str], tuple[bool, bool]]:
    if rollout_dir is None:
        return {}
    path = Path(rollout_dir) / "audit_overrides.json"
    if not path.is_file():
        return {}
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError(f"{path}: audit_overrides.json must be a JSON list of override objects")
    overrides: dict[tuple[str, str], tuple[bool, bool]] = {}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("stage_id"), str)
            or not entry["stage_id"].strip()
            or not isinstance(entry.get("id"), str)
            or not entry["id"].strip()
            or not isinstance(entry.get("original_passed"), bool)
            or not isinstance(entry.get("passed"), bool)
            or not isinstance(entry.get("reason"), str)
            or not entry["reason"].strip()
        ):
            raise ValueError(
                f"{path}: each override must specify non-empty 'stage_id', 'id', boolean 'original_passed' and 'passed', and 'reason'"
            )
        sid, cid = entry["stage_id"], entry["id"]
        if sid not in planned or cid not in (planned[sid][0] | planned[sid][1]):
            raise ValueError(f"{path}: unknown stage/item override {sid}/{cid}")
        if (sid, cid) in overrides:
            raise ValueError(f"{path}: duplicate override for {sid}/{cid}")
        overrides[(sid, cid)] = (entry["original_passed"], entry["passed"])
    return overrides


def _apply_audit_overrides(
    last: dict[str, list[dict]],
    overrides: dict[tuple[str, str], tuple[bool, bool]],
) -> dict[str, list[dict]]:
    if not overrides:
        return last
    out: dict[str, list[dict]] = {}
    for sid, items in last.items():
        updated = []
        seen: set[str] = set()
        for item in items:
            cid = item["id"]
            seen.add(cid)
            if (sid, cid) in overrides:
                orig_passed, new_passed = overrides[(sid, cid)]
                if item["passed"] is not orig_passed:
                    raise ValueError(
                        f"Override {sid}/{cid} expected original_passed={orig_passed}, "
                        f"but rollout report has passed={item['passed']}"
                    )
                updated.append({**item, "passed": new_passed})
            else:
                updated.append(item)
        for o_sid, o_cid in overrides:
            if o_sid == sid and o_cid not in seen:
                raise ValueError(f"Override target {o_sid}/{o_cid} was not evaluated in rollout report")
        out[sid] = updated
    for o_sid, o_cid in overrides:
        if o_sid not in out:
            raise ValueError(f"Override target stage {o_sid!r} ({o_cid}) has no formal evaluation in rollout report")
    return out


def stage_scores(
    report: dict,
    task: dict,
    design: dict | None = None,
    *,
    rollout_dir: Path | None = None,
) -> dict[str, float]:
    if "status" in report and report["status"] not in SCORABLE_STATUSES:
        raise ValueError(
            f"Rollout status {report['status']!r} is not scorable (expected one of {SCORABLE_STATUSES})"
        )
    resolved_design = _resolve_validated_design(task, design, rollout_dir)
    planned = {
        s["id"]: ({c["id"] for c in s["verifier_checks"]}, {c["id"] for c in s["criteria"]})
        for s in task["stages"]
    }
    total = sum(len(checks) + len(criteria) for checks, criteria in planned.values())
    if not total:
        raise ValueError("Task has no evaluation checks or criteria")
    overrides = _load_audit_overrides(rollout_dir, planned)
    last = _apply_audit_overrides(last_evaluations(report), overrides)
    passed_checks = passed_criteria = n_checks = n_criteria = 0
    for sid, (checks, criteria) in planned.items():
        ok = {c["id"] for c in last.get(sid, []) if c["passed"]}
        passed_checks += len(checks & ok)
        passed_criteria += len(criteria & ok)
        n_checks += len(checks)
        n_criteria += len(criteria)
    items_rate = (passed_checks + passed_criteria) / total
    rewards = {**core_minor_scores(last, task, resolved_design), "items": items_rate}
    if n_checks:
        rewards["checks"] = passed_checks / n_checks
    if n_criteria:
        rewards["criteria"] = passed_criteria / n_criteria
    return rewards


def score_rollout(rollout_dir: str | Path, *, design: dict | None = None) -> dict[str, float]:
    """Recompute structured rewards from a saved terminal `rollout/` directory using its frozen `design.json`."""
    d = Path(rollout_dir)
    report = json.loads((d / "report.json").read_text(encoding="utf-8"))
    if report.get("status") not in SCORABLE_STATUSES:
        raise ValueError(
            f"Rollout status {report.get('status')!r} is not scorable (expected one of {SCORABLE_STATUSES})"
        )
    task = json.loads((d / "task.json").read_text(encoding="utf-8"))
    return stage_scores(report, task, design, rollout_dir=d)
