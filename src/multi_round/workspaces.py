"""Worker workspace boundary; evidence archiving and User/Reviewer directories remain managed locally."""

import shutil
import sys
from pathlib import Path, PurePath
from typing import Protocol

from .models import inside
from .processes import run_process
from .storage import snapshot


class WorkerWorkspace(Protocol):
    root: PurePath

    def publish(self, source: Path, relative: str) -> None: ...

    def fetch_file(self, relative: str, target: Path) -> None: ...

    def collect(self, paths: list[str], destination: Path) -> dict[str, str]: ...

    def hook_bindings(self, task_root: Path) -> dict[str, str]: ...

    def hook_context(self, context: dict) -> dict: ...

    def execute(
        self, argv: list[str], *, stdin: str, timeout: float, log_dir: Path
    ) -> str: ...


class LocalWorkspace:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def publish(self, source: Path, relative: str) -> None:
        target = inside(self.root, relative)
        if target.exists():
            raise ValueError(f"Publishing attachment would overwrite existing file: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    def fetch_file(self, relative: str, target: Path) -> None:
        shutil.copy2(inside(self.root, relative), target)

    def collect(self, paths: list[str], destination: Path) -> dict[str, str]:
        return snapshot(self.root, destination, paths)

    def hook_bindings(self, task_root: Path) -> dict[str, str]:
        return {"{python}": sys.executable, "{task}": str(task_root)}

    def hook_context(self, context: dict) -> dict:
        return context

    def execute(
        self, argv: list[str], *, stdin: str, timeout: float, log_dir: Path
    ) -> str:
        return run_process(
            argv, cwd=self.root, stdin=stdin, timeout=timeout, log_dir=log_dir
        )
