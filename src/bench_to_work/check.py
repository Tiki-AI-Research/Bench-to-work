"""Offline task integrity and reference-solution verification (no container build, no LLM calls).

Checks per task:
1. Harbor `Task` loads `task.toml`, `instruction.md`, `environment/Dockerfile`, `tests/test.sh`.
2. `multi_round.models.load_task` validates `spec/task.json`.
3. Task hooks and verifier conform to container isolation boundaries.
4. `environment/` contains only expected container runtime files and matching `public/` hook scripts.
5. `spec/manifest.json` SHA-256 checksums match every file under `spec/`.
6. Every stage declaring `verifier_checks` includes `spec/verify/reference/<stage_id>/` and passes `spec/verifier.py` 100%.
7. Public preview hooks (if configured) generate `.review/preview.md` cleanly.
8. Two-tier outcome scoring metadata (`spec/verify/design.json`) is present, complete, and yields reward 1.0 on full pass.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

from . import REPO, SPEC_DIR
from .rewards import DEFAULT_MINOR_WEIGHT, ROLES, RULE, stage_scores, tagged
from .tasks import find, resolve

TEMPLATES = Path(__file__).resolve().parent / "templates"
ENVIRONMENT_FILES = ("Dockerfile", "worker.py", "requirements.txt", "check_env.py")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def container_isolation_issues(task: dict) -> list[str]:
    """Validate that hooks do not depend on controller-only placeholders or host-only handlers."""
    out = []
    for h in task.get("hooks") or []:
        if h.get("handler"):
            out.append(f"Hook {h['id']} uses a Python handler (unsupported inside Worker container); use a command hook")
        command = " ".join(h.get("command") or [])
        if re.search(r"\{(turn_dir|log_dir|stage)\}", command):
            out.append(f"Hook {h['id']} references controller-only placeholders unavailable inside container: {command}")
    return out


def check_harbor(task_dir: Path) -> list[str]:
    from harbor.models.task.task import Task

    try:
        task = Task(task_dir)
    except Exception as exc:  # noqa: BLE001
        return [f"Harbor Task failed to load: {type(exc).__name__}: {exc}"]
    out = []
    expected = f"{SPEC_DIR}/task.json"
    if task.config.metadata.get("task_spec") != expected:
        out.append(f"task.toml metadata.task_spec must be '{expected}'")
    for rel in ("instruction.md", "environment/Dockerfile", "tests/test.sh"):
        if not (task_dir / rel).is_file():
            out.append(f"Missing {rel}")
    return out


def check_spec(task_dir: Path) -> list[str]:
    from multi_round.models import load_task

    try:
        load_task(task_dir / SPEC_DIR / "task.json")
    except Exception as exc:  # noqa: BLE001
        return [f"Task spec failed to load: {type(exc).__name__}: {exc}"]
    return []


def _extract_searchable_text(path: Path) -> str:
    if zipfile.is_zipfile(path):
        chunks = []
        with zipfile.ZipFile(path) as zf:
            for name in zf.namelist():
                if name.endswith((".xml", ".rels", ".txt", ".json", ".csv", ".md", ".py")):
                    chunks.append(zf.read(name).decode("utf-8", errors="ignore"))
        return "\n".join(chunks)
    return path.read_text(encoding="utf-8", errors="ignore")


def check_public(task_dir: Path, task: dict) -> list[str]:
    env = task_dir / "environment"
    pub_dir = env / "public"
    spec_dir = task_dir / SPEC_DIR
    meta = tomllib.loads((task_dir / "task.toml").read_text(encoding="utf-8")).get("metadata", {})
    declared = list(meta.get("environment_extra") or [])
    out = []
    extra = sorted(p.name for p in env.iterdir() if p.name not in (*ENVIRONMENT_FILES, "public", *declared))
    if extra:
        out.append(f"Unexpected files in environment/ (would enter container image): {extra}")
    if missing := [name for name in declared if not (env / name).exists()]:
        out.append(f"Declared environment_extra missing from environment/: {missing}")
    if not pub_dir.is_dir():
        out.append("Missing environment/public/ directory")
        return out

    private_dirs = {"verify", "materials"}
    private_spec_files = {"task.json", "verifier.py", "manifest.json", "README.md"}
    seen_private_dir: set[str] = set()
    for entry in sorted(pub_dir.rglob("*")):
        rel_entry = entry.relative_to(pub_dir)
        rel_posix = rel_entry.as_posix()
        if entry.is_symlink():
            out.append(f"environment/public/{rel_posix} must not be a symlink")
        if hit := next((part for part in rel_entry.parts if part in private_dirs), None):
            if f"{rel_posix}:{hit}" not in seen_private_dir:
                seen_private_dir.add(f"{rel_posix}:{hit}")
                out.append(f"environment/public/{rel_posix} exposes controller-private directory '{hit}/'")
        if entry.is_file() and rel_entry.name in private_spec_files:
            out.append(f"environment/public/{rel_posix} exposes controller-private spec file '{rel_entry.name}'")

    referenced = []
    for h in task.get("hooks") or []:
        for arg in h.get("command") or []:
            if arg.startswith("{task}/"):
                referenced.append(arg.removeprefix("{task}/"))
    have = {p.relative_to(pub_dir).as_posix() for p in pub_dir.rglob("*") if p.is_file() and not p.is_symlink()} - {".keep"}
    for rel in referenced:
        if rel not in have:
            out.append(f"environment/public/ is missing hook script '{rel}'")
    if not referenced and have:
        out.append(f"environment/public/ contains files {sorted(have)} even though task declares no '{'{task}/...'}' hooks")
    for rel in sorted(have):
        if not (spec_dir / rel).is_file():
            out.append(f"environment/public/{rel} has no matching source in {SPEC_DIR}/{rel}")
        elif sha256(pub_dir / rel) != sha256(spec_dir / rel):
            out.append(f"environment/public/{rel} differs from {SPEC_DIR}/{rel}")

    item_ids = {
        c["id"]
        for s in task.get("stages") or []
        for c in (s.get("verifier_checks") or []) + (s.get("criteria") or [])
        if isinstance(c, dict) and isinstance(c.get("id"), str) and c["id"]
    }
    if item_ids:
        id_pat = re.compile(
            r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(cid) for cid in sorted(item_ids)) + r")(?![A-Za-z0-9_])"
        )
        for rel in sorted(have):
            text = _extract_searchable_text(pub_dir / rel)
            if leaked := sorted(set(id_pat.findall(text))):
                out.append(f"environment/public/{rel} leaks private check/criterion IDs: {leaked[:5]}")
    return out


def check_manifest(task_dir: Path) -> list[str]:
    spec_dir = task_dir / SPEC_DIR
    if not (spec_dir / "manifest.json").is_file():
        return [f"Missing {SPEC_DIR}/manifest.json"]
    files = json.loads((spec_dir / "manifest.json").read_text(encoding="utf-8"))["files"]
    actual = {
        p.relative_to(spec_dir).as_posix(): p
        for p in spec_dir.rglob("*")
        if p.is_file() and p.name != "manifest.json" and "__pycache__" not in p.parts
    }
    out = [f"manifest missing entries: {sorted(set(actual) - set(files))[:5]}"] if set(actual) - set(files) else []
    out += [f"manifest lists nonexistent files: {sorted(set(files) - set(actual))[:5]}"] if set(files) - set(actual) else []
    out += [f"SHA-256 mismatch: {rel}" for rel in sorted(set(files) & set(actual)) if sha256(actual[rel]) != files[rel]]
    return out


def expand_verifier_cmd(task: dict, spec_dir: Path, workspace: Path, stage: str, turn_dir: Path) -> list[str]:
    template = task.get("verifier") or ["{python}", "{task}/verifier.py", "--workspace", "{workspace}", "--stage", "{stage}"]
    mapping = {
        "{python}": sys.executable,
        "{project}": str(REPO),
        "{task}": str(spec_dir),
        "{workspace}": str(workspace),
        "{stage}": stage,
        "{turn_dir}": str(turn_dir),
        "{log_dir}": str(turn_dir / "verifier"),
    }
    cmd = []
    for arg in template:
        val = arg
        for k, v in mapping.items():
            val = val.replace(k, v)
        cmd.append(val)
    if cmd and cmd[0] == sys.executable and "-B" not in cmd[1:2]:
        cmd.insert(1, "-B")
    return cmd


def run_verifier(task: dict, spec_dir: Path, workspace: Path, stage: str, turn_dir: Path) -> dict:
    cmd = expand_verifier_cmd(task, spec_dir, workspace, stage, turn_dir)
    p = subprocess.run(cmd, capture_output=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", errors="replace")[-1500:])
    return json.loads(p.stdout.decode("utf-8"))


def stage_attachments(stage: dict) -> list[tuple[str, str]]:
    out = []
    for item in stage.get("initial", {}).get("attachments") or []:
        if isinstance(item, str):
            out.append((item, item))
        elif isinstance(item, dict) and item.get("path"):
            src = item["path"]
            dst = item.get("target") or item.get("workspace_path") or src
            out.append((src, dst))
    return out


def check_answers(task_dir: Path, task: dict, work: Path) -> tuple[list[str], dict]:
    """Run `spec/verifier.py` against `spec/verify/reference/<stage_id>/` for every stage with programmatic checks."""
    spec_dir = task_dir / SPEC_DIR
    ref_root = spec_dir / "verify" / "reference"
    checks_json = spec_dir / "verify" / "checks.json"
    checks_spec = json.loads(checks_json.read_text(encoding="utf-8")) if checks_json.is_file() else None

    out, scores = [], {}
    cumulative_atts: list[tuple[str, str]] = []
    prev_sid: str | None = None

    for st in task["stages"]:
        sid = st["id"]
        cumulative_atts.extend(stage_attachments(st))
        declared = st.get("verifier_checks") or []
        declared_ids = [c["id"] for c in declared]
        reference = ref_root / sid
        if not declared_ids:
            if reference.is_dir():
                prev_sid = sid
            continue
        if not reference.is_dir():
            scores[sid] = [0, len(declared_ids)]
            out.append(
                f"{task_dir.name}/{sid}: missing required reference directory "
                f"'{SPEC_DIR}/verify/reference/{sid}/' for {len(declared_ids)} declared verifier_checks"
            )
            continue

        ws, turns = work / sid / "ws", work / sid / "run" / "turns"
        ws.mkdir(parents=True, exist_ok=True)
        if checks_spec and sid in checks_spec.get("stages", {}):
            st_spec = checks_spec["stages"][sid]
            for m in st_spec.get("materials", []):
                (ws / m["file"]).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(spec_dir / m["file"], ws / m["file"])
            prev_for_turn = st_spec.get("previous")
        else:
            for src_rel, dst_rel in cumulative_atts:
                src_p = spec_dir / src_rel
                if src_p.is_file():
                    (ws / dst_rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_p, ws / dst_rel)
            prev_for_turn = prev_sid

        shutil.copytree(reference, ws, dirs_exist_ok=True)
        if prev_for_turn and (ref_root / prev_for_turn).is_dir():
            shutil.copytree(ref_root / prev_for_turn, turns / f"{prev_for_turn}-01" / "submission")
        (turns / f"{sid}-01").mkdir(parents=True, exist_ok=True)

        try:
            res = run_verifier(task, spec_dir, ws, sid, turns / f"{sid}-01")
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            err_msg = str(exc)[-300:]
            scores[sid] = [0, len(declared_ids)]
            if "soffice" in err_msg and shutil.which("soffice") is None:
                out.append(f"{task_dir.name}/{sid}: reference workspace requires LibreOffice (`soffice` not found in PATH)")
            else:
                out.append(f"{task_dir.name}/{sid}: reference workspace caused verifier error: {err_msg}")
            prev_sid = sid
            continue

        raw_checks = res.get("checks") if isinstance(res, dict) else None
        if not isinstance(raw_checks, list):
            scores[sid] = [0, len(declared_ids)]
            out.append(f"{task_dir.name}/{sid}: verifier output must contain a 'checks' list")
            prev_sid = sid
            continue

        bad_entries = [
            repr(c)
            for c in raw_checks
            if not isinstance(c, dict) or not isinstance(c.get("id"), str) or type(c.get("passed")) is not bool
        ]
        returned_ids = [c["id"] for c in raw_checks if isinstance(c, dict) and isinstance(c.get("id"), str)]
        dups = sorted({cid for cid in returned_ids if returned_ids.count(cid) > 1})
        missing = [cid for cid in declared_ids if cid not in set(returned_ids)]
        extra = sorted(set(returned_ids) - set(declared_ids))

        if bad_entries or dups or missing or extra:
            details = []
            if missing:
                details.append(f"missing={missing}")
            if dups:
                details.append(f"duplicate={dups}")
            if extra:
                details.append(f"unexpected={extra}")
            if bad_entries:
                details.append(f"invalid_entries={bad_entries[:3]}")
            valid_passed = {
                c["id"]
                for c in raw_checks
                if isinstance(c, dict)
                and c.get("id") in set(declared_ids)
                and c.get("id") not in set(dups)
                and c.get("passed") is True
            }
            scores[sid] = [len(valid_passed), len(declared_ids)]
            out.append(f"{task_dir.name}/{sid}: verifier check ID mismatch ({', '.join(details)})")
        else:
            failed = [c for c in raw_checks if not c["passed"]]
            scores[sid] = [len(declared_ids) - len(failed), len(declared_ids)]
            out += [
                f"{task_dir.name}/{sid}: reference workspace failed {c['id']}: {str(c.get('evidence', ''))[:200]}"
                for c in failed
            ]
        prev_sid = sid
    return out, scores


def check_preview(task_dir: Path, task: dict, work: Path) -> list[str]:
    references = sorted((task_dir / SPEC_DIR / "verify" / "reference").glob("*")) if (task_dir / SPEC_DIR / "verify" / "reference").is_dir() else []
    if not references or not task.get("hooks"):
        return []
    preview_hook = next(
        (
            h
            for h in (task.get("hooks") or [])
            if len(h.get("command") or []) >= 3
            and str(h["command"][1]).startswith("{task}/")
            and "preview" in h["command"]
        ),
        None,
    )
    if not preview_hook:
        return []
    rel_script = preview_hook["command"][1].removeprefix("{task}/")
    script_path = task_dir / "environment" / "public" / rel_script
    if not script_path.is_file():
        return [f"Missing public preview hook script: {script_path}"]
    ws = work / "preview"
    shutil.copytree(references[-1], ws)
    p = subprocess.run(
        [sys.executable, "-B", str(script_path), *preview_hook["command"][2:]],
        input=json.dumps({"workspace": str(ws)}).encode("utf-8"),
        capture_output=True,
        timeout=300,
    )
    if p.returncode != 0:
        return [f"Public preview hook failed: {p.stderr.decode('utf-8', errors='replace')[-500:]}"]
    text = (ws / ".review" / "preview.md").read_text(encoding="utf-8") if (ws / ".review" / "preview.md").is_file() else ""
    return [] if ("## " in text or text.strip()) else ["Public preview hook produced empty .review/preview.md"]


def check_scoring(task_dir: Path, task: dict) -> list[str]:
    spec_dir = task_dir / SPEC_DIR
    if not (spec_dir / "verify" / "design.json").is_file():
        return [f"Missing {SPEC_DIR}/verify/design.json (required for {RULE} scoring)"]
    design = json.loads((spec_dir / "verify" / "design.json").read_text(encoding="utf-8"))
    if not tagged(design):
        return [f"design.json scoring.rule must be '{RULE}'"]
    minor_weight = (design.get("scoring") or {}).get("minor_weight", DEFAULT_MINOR_WEIGHT)
    if not isinstance(minor_weight, (int, float)) or not (0.0 <= float(minor_weight) < 1.0):
        return [f"design.json scoring.minor_weight must be in [0.0, 1.0), got {minor_weight!r}"]
    by_id = {s["id"]: s for s in design["stages"]}
    out = []
    for s in task["stages"]:
        ids = [c["id"] for c in s["verifier_checks"] + s["criteria"]]
        d = by_id.get(s["id"], {})
        tags = {it["id"]: it for it in d.get("items", [])}
        if extra := sorted(set(tags) - set(ids)):
            out.append(f"{s['id']}: design.json lists unknown check IDs {extra}")
        if not ids:
            continue
        setup = d.get("scored", True) is False
        if setup and not d.get("setup"):
            out.append(f"{s['id']}: setup stage (scored: false) must explain setup purpose")
        for c in s["verifier_checks"] + s["criteria"]:
            it = tags.get(c["id"], {})
            role = it.get("role")
            if role not in ROLES:
                out.append(f"{s['id']}/{c['id']}: role must be one of {ROLES}")
                continue
            if "group" in it:
                out.append(f"{s['id']}/{c['id']}: unsupported 'group' field in {RULE}; specify 'role' ({ROLES})")
        has_core = any(tags.get(i, {}).get("role") == "core" for i in ids)
        if not setup and not has_core:
            out.append(f"{s['id']}: scored stage must include at least one core item")
        if setup and has_core:
            out.append(f"{s['id']}: setup stage must not contain core items")
    if not out:
        everything = {
            "stages": [
                {
                    "id": s["id"],
                    "turns": [{"evaluation": [{"id": c["id"], "passed": True} for c in s["verifier_checks"] + s["criteria"]]}],
                }
                for s in task["stages"]
            ]
        }
        scores = stage_scores(everything, task, design)
        if abs(scores["reward"] - 1.0) > 1e-9 or scores.get("task_passed") != 1.0:
            out.append(f"Full-pass synthetic run yielded {scores}, expected reward=1.0 and task_passed=1.0")
    return out


def check_task(task_dir: Path) -> dict:
    task = json.loads((task_dir / SPEC_DIR / "task.json").read_text(encoding="utf-8"))
    problems = (
        check_harbor(task_dir)
        + check_spec(task_dir)
        + container_isolation_issues(task)
        + check_public(task_dir, task)
        + check_manifest(task_dir)
        + check_scoring(task_dir, task)
    )
    with tempfile.TemporaryDirectory(prefix="btw-check-") as tmp:
        found, scores = check_answers(task_dir, task, Path(tmp) / "answers")
        problems += found + check_preview(task_dir, task, Path(tmp))
    return {"task": task_dir.name, "stages": len(task["stages"]), "answers": scores, "problems": problems}


def main(argv=None) -> int:
    if os.name == "nt" and not sys.flags.utf8_mode and argv is None:
        return subprocess.call([sys.executable, "-X", "utf8", "-m", "bench_to_work.check", *sys.argv[1:]])
    ap = argparse.ArgumentParser(description="Offline integrity and reference-solution check (no Docker, no LLM calls)")
    ap.add_argument("tasks", nargs="*", help="Task ID(s) to check; omit to check all tasks under --dataset")
    ap.add_argument("--dataset", default=str(REPO / "tasks"))
    args = ap.parse_args(argv)
    dataset = Path(args.dataset)
    code = 0
    dirs = []
    for t in args.tasks:
        try:
            dirs.append(resolve(dataset, t))
        except (FileNotFoundError, ValueError) as exc:
            print(f"{t}: {exc}")
            code = 1
    if not args.tasks:
        dirs = find(dataset)
        if not dirs:
            print(f"No tasks found in dataset ({dataset})", file=sys.stderr)
            return 2
    for d in dirs:
        if not (d / SPEC_DIR / "task.json").is_file():
            print(f"{d.name}: missing {SPEC_DIR}/task.json")
            code = 1
            continue
        r = check_task(d)
        answers = ", ".join(f"{s} {a}/{b}" for s, (a, b) in r["answers"].items()) or "no reference workspaces"
        verdict = f"{len(r['problems'])} issue(s)" if r["problems"] else "PASSED"
        print(f"{r['task']}: {r['stages']} stages; references [{answers}]; {verdict}")
        for p in r["problems"]:
            print(f"  - {p}")
        code = code or (1 if r["problems"] else 0)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
