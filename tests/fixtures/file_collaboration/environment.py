"""Task-specific workspace hook: concurrent user edit, simulated file lock, and unlock."""

import json
from pathlib import Path
import sys


def main():
    context = json.load(sys.stdin)
    workspace = Path(context["workspace"])
    directory = workspace / "shared"
    target = directory / "report.json"
    operation = sys.argv[1]
    if operation == "edit":
        data = json.loads(target.read_text(encoding="utf-8"))
        data["note"] = "User update: keep offline orders"
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    elif operation == "lock":
        # Simulate write failure via read-only file and directory permissions on POSIX.
        target.chmod(0o444)
        directory.chmod(0o555)
    elif operation in {"close", "cleanup"}:
        if directory.exists():
            directory.chmod(0o755)
        if target.exists():
            target.chmod(0o644)
    else:
        raise ValueError(f"Unknown environment operation: {operation}")
    print(json.dumps({"operation": operation, "status": "completed"}))


if __name__ == "__main__":
    main()
