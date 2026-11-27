"""Task discovery: any directory under tasks/ containing a task.toml is a benchmark task.

Tasks may be stored flat (tasks/my-task) or grouped by domain/category (tasks/finance/my-task).
"""
from __future__ import annotations

from pathlib import Path


def find(dataset: Path) -> list[Path]:
    """Return all task directories under `dataset` in deterministic lexicographic order."""
    found: list[Path] = []

    def walk(directory: Path) -> None:
        for path in sorted(directory.iterdir(), key=lambda p: p.name):
            if not path.is_dir() or path.name.startswith("."):
                continue
            if (path / "task.toml").is_file():
                found.append(path)
            else:
                walk(path)

    if dataset.is_dir():
        walk(dataset)
    return found


def resolve(dataset: Path, name: str) -> Path:
    """Resolve a task ID or relative path (e.g. 'fy26-merit-promotion-calibration' or 'finance/quarterly-close') to its directory."""
    direct = dataset / name
    if (direct / "task.toml").is_file():
        return direct
    hits = [p for p in find(dataset) if p.name == name]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise FileNotFoundError(f"Task not found in dataset: {name} ({dataset})")
    joined = ", ".join(p.relative_to(dataset).as_posix() for p in hits)
    raise ValueError(f"Multiple tasks named {name}: {joined}; specify the relative group path")
