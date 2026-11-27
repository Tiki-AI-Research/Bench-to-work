"""Operate on a Harbor-managed environment without owning container lifecycle or stage scheduling."""

import asyncio
import json
import math
import shlex
import sys
import tempfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

from harbor.environments.base import ExecResult

from multi_round.models import ExternalExecutionError
from multi_round.storage import snapshot

from .bridge import EnvironmentBridge

# In-container collection helper: validates boundaries and copies declared paths without carrying task rules or future materials.
COLLECT = """
import json, pathlib, shutil, sys
root, target = map(pathlib.Path, sys.argv[1:3])
target.mkdir(parents=True)
selected = set()
for name in json.loads(sys.argv[3]):
    path = root / name
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Submission path escapes workspace or is a symlink: ' + name)
    selected.add(path)
    if path.is_dir():
        selected.update(path.rglob('*'))
for path in sorted(selected):
    if path.is_symlink():
        raise ValueError('Submission contains a symlink: ' + str(path))
    if path.is_file():
        dest = target / path.relative_to(root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
"""


class HarborWorkspace:
    def __init__(
        self,
        environment,
        bridge: EnvironmentBridge,
        root="/workspace",
        python="python3",
        public_task: str | None = None,
        transfer_timeout=60,
    ):
        self.environment, self.bridge = environment, bridge
        self.root = PurePosixPath(root)
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise ValueError("Worker workspace root must be an absolute path inside the environment")
        self.python, self.public_task = python, public_task
        self.transfer_timeout = transfer_timeout

    def path(self, relative: str) -> PurePosixPath:
        value = PurePosixPath(relative)
        if value.is_absolute() or ".." in value.parts:
            raise ValueError(f"Workspace relative path escapes root: {relative}")
        return self.root / value

    async def transfer(self, operation, *args):
        try:
            async with asyncio.timeout(self.transfer_timeout):
                return await operation(*args)
        except Exception as exc:
            raise ExternalExecutionError(f"Harbor file transfer failed: {exc}") from exc

    async def command(self, argv, *, timeout, stdin="", env: dict[str, str] | None = None):
        # Stage stdin via a temp file so long prompts never hit shell argument length limits.
        remote = f"/tmp/harbor-command-{uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="harbor-input-") as directory:
            local = Path(directory) / "input"
            local.write_text(stdin, encoding="utf-8")
            await self.transfer(self.environment.upload_file, local, remote + ".input")
            command = shlex.join(["timeout", "--kill-after=5", f"{timeout}s", *argv])
            # Redirect stdout and stderr separately inside the container so diagnostics never corrupt JSON stdout.
            shell = f"{command} < {remote}.input > {remote}.stdout 2> {remote}.stderr"
            result = None
            try:
                exec_kwargs = {"cwd": str(self.root), "timeout_sec": math.ceil(timeout + 10)}
                if env:
                    exec_kwargs["env"] = dict(env)
                result = await self.environment.exec(shell, **exec_kwargs)
                for stream in ("stdout", "stderr"):
                    await self.transfer(
                        self.environment.download_file,
                        f"{remote}.{stream}",
                        Path(directory) / stream,
                    )
                result = ExecResult(
                    return_code=result.return_code,
                    stdout=(Path(directory) / "stdout").read_text(encoding="utf-8"),
                    stderr=(Path(directory) / "stderr").read_text(encoding="utf-8"),
                )
                return result
            except Exception as exc:
                raise ExternalExecutionError(f"Harbor command execution failed: {exc}") from exc
            finally:
                # Remove per-call temp files without masking any primary execution error.
                original = sys.exception()
                try:
                    cleanup = await self.environment.exec(
                        f"rm -f {remote}.input {remote}.stdout {remote}.stderr",
                        timeout_sec=10,
                    )
                    if cleanup.return_code != 0:
                        raise ExternalExecutionError(
                            f"Failed to remove remote command temp files: {remote}"
                        )
                except Exception as exc:
                    if original is not None:
                        original.add_note(f"Remote temp file cleanup failed: {exc}")
                    elif result is not None and result.return_code != 0:
                        result.stderr = (result.stderr or "") + f"\nCleanup also failed: {exc}"
                    else:
                        raise

    async def checked(self, argv):
        # Lightweight helper for file checks/copies that take no stdin and produce no JSON protocol stdout.
        command = shlex.join(
            ["timeout", "--kill-after=5", f"{self.transfer_timeout}s", *argv]
        )
        try:
            result = await self.environment.exec(
                command,
                cwd=str(self.root),
                timeout_sec=math.ceil(self.transfer_timeout + 10),
            )
        except Exception as exc:
            raise ExternalExecutionError(f"Workspace operation execution failed: {exc}") from exc
        if result.return_code != 0:
            raise ExternalExecutionError(
                f"Workspace operation failed (exit code {result.return_code}): {result.stderr or result.stdout}"
            )
        return result

    def publish(self, source: Path, relative: str) -> None:
        target = str(self.path(relative))

        async def publish():
            await self.checked(
                [
                    self.python,
                    "-c",
                    """
import pathlib, sys
root, path = map(pathlib.Path, sys.argv[1:])
if not path.resolve().is_relative_to(root.resolve()) or path.exists() or path.is_symlink():
    raise ValueError('Attachment path escapes workspace or overwrites existing file: ' + str(path))
path.parent.mkdir(parents=True, exist_ok=True)
""",
                    str(self.root),
                    target,
                ]
            )
            await self.transfer(self.environment.upload_file, source, target)

        self.bridge.call(publish)

    def fetch_file(self, relative: str, target: Path) -> None:
        remote = str(self.path(relative))

        async def fetch():
            await self.checked(
                [
                    self.python,
                    "-c",
                    """
import pathlib, sys
root, path = map(pathlib.Path, sys.argv[1:])
if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink() or not path.is_file():
    raise ValueError('Published attachment file is unavailable: ' + str(path))
""",
                    str(self.root),
                    remote,
                ]
            )
            await self.transfer(self.environment.download_file, remote, target)

        self.bridge.call(fetch)

    def collect(self, paths: list[str], destination: Path) -> dict[str, str]:
        for name in paths:
            self.path(name)

        async def collect(local):
            remote = f"/tmp/harbor-submission-{uuid4().hex}"
            try:
                await self.checked(
                    [
                        self.python,
                        "-c",
                        COLLECT,
                        str(self.root),
                        remote,
                        json.dumps(paths),
                    ]
                )
                await self.transfer(self.environment.download_dir, remote, local)
            finally:
                original = sys.exception()
                try:
                    await self.checked(["rm", "-rf", remote])
                except Exception as exc:
                    if original is None:
                        raise
                    original.add_note(f"Remote submission temp dir cleanup failed: {exc}")

        # Download into an isolated local temp directory, then compute manifests via shared snapshot().
        with tempfile.TemporaryDirectory(prefix="harbor-submission-") as directory:
            local = Path(directory) / "files"
            self.bridge.call(collect, local)
            return snapshot(local, destination)

    def hook_bindings(self, task_root: Path) -> dict[str, str]:
        values = {"{python}": self.python}
        if self.public_task is not None:
            values["{task}"] = self.public_task
        return values

    def hook_context(self, context: dict) -> dict:
        # Remote commands have no access to controller-side log_dir/turn_dir paths; stdout/stderr are captured automatically.
        return {**context, "log_dir": None, "turn_dir": None}

    def execute(self, argv, *, stdin, timeout, log_dir, env: dict[str, str] | None = None):
        if any("{task}" in arg for arg in argv):
            raise ValueError(
                "Remote hook uses {task}, but public_task directory is not configured"
            )
        result = self.bridge.call(self.command, argv, stdin=stdin, timeout=timeout, env=env)
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "stdout.log").write_text(result.stdout or "", encoding="utf-8")
        (log_dir / "stderr.log").write_text(result.stderr or "", encoding="utf-8")
        if result.return_code != 0:
            raise ExternalExecutionError(
                f"Harbor command exited with code {result.return_code}; see {log_dir}"
            )
        return result.stdout or ""
