"""File evidence persistence: submission snapshots, attachment archiving, role inboxes, and atomic JSON writes."""

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from .models import Attachment, inside


def atomic_write_text(path: Path, text: str) -> None:
    """Write completely to a sibling temp file then replace atomically so readers never observe partial files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Create temp file on the same filesystem and close before replace for Windows compatibility.
    with TemporaryDirectory(dir=path.parent, prefix=f".{path.name}.") as temporary:
        staged = Path(temporary) / path.name
        staged.write_text(text, encoding="utf-8")
        staged.replace(path)


def write_json(path: Path, value) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prepare_review_workspace(
    directory: Path, messages: list[dict], submitted: Path | None
) -> None:
    """Create an isolated point-in-time evaluation directory so later turns cannot mutate reviewed evidence."""
    evaluation = directory / "evaluation"
    evaluation.mkdir(parents=True)
    (directory / "scratch").mkdir()
    with (evaluation / "conversation.jsonl").open("w", encoding="utf-8") as stream:
        for index, message in enumerate(messages):
            stream.write(
                json.dumps({**message, "index": index}, ensure_ascii=False) + "\n"
            )
    if submitted is not None:
        # Copy the frozen submission into review_workspace; read-only enforcement is handled by the adapter.
        shutil.copytree(submitted, directory / "submission")


def snapshot(
    source: Path, target: Path, paths: list[str] | None = None
) -> dict[str, str]:
    """Collect only declared submission_paths, excluding tool caches/venvs and rejecting symlinks within scope."""
    hashes = {}
    selected = set()
    for name in paths or ["."]:
        if (source / name).is_symlink():
            raise ValueError(f"Submission path is a symlink: {name}")
        base = inside(source, name)
        selected.add(base)
        if base.is_dir():
            selected.update(base.rglob("*"))
    for path in sorted(selected):
        if path.is_symlink():
            raise ValueError(
                f"Submission contains a symlink: {path.relative_to(source)}"
            )
        if path.is_file():
            relative = path.relative_to(source)
            dest = target / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
            hashes[str(relative)] = hashlib.sha256(dest.read_bytes()).hexdigest()
    target.mkdir(parents=True, exist_ok=True)
    return hashes


def archive_attachments(
    attachments: list[Attachment],
    fetch_file: Callable[[str, Path], None],
    directory: Path,
    run_dir: Path,
) -> list[dict]:
    """Archive exact copies of attachments at send time so subsequent Worker edits cannot alter input evidence."""
    records = []
    for index, attachment in enumerate(attachments):
        target = directory / f"{index:02d}-{Path(attachment.path).name}"
        target.parent.mkdir(parents=True, exist_ok=True)
        fetch_file(attachment.path, target)
        records.append(
            {
                "path": attachment.path,
                "kind": attachment.kind,
                "saved_path": str(target.relative_to(run_dir)),
            }
        )
    return records


def conversation_images(report: dict, run_dir: Path) -> list[Attachment]:
    """Return archived User-sent images across prior turns; Worker image outputs are read from submission/ via tools."""
    return [
        Attachment(str(run_dir / a["saved_path"]), "image")
        for stage in report["stages"]
        for turn in stage["turns"]
        for a in turn.get("user_attachments", [])
        if a["kind"] == "image"
    ]


def receive_attachments(
    records: list[dict], run_dir: Path, user_workspace: Path
) -> None:
    """Copy archived User attachments into user_workspace without syncing live Worker edits."""
    for record in records:
        target = inside(user_workspace, record["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        shutil.copy2(run_dir / record["saved_path"], target)


def receive_submission(submitted: Path, user_workspace: Path, paths: list[str]) -> None:
    """Replace declared submission_paths in user_workspace so deleted deliverables do not linger from earlier turns."""
    for name in paths:
        target = inside(user_workspace, name)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    user_workspace.mkdir(parents=True, exist_ok=True)
    shutil.copytree(submitted, user_workspace, dirs_exist_ok=True)
