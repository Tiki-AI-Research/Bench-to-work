"""Programmatic deliverable check; clarification and lock-release dialogue are checked by stage rubrics."""

import json
from pathlib import Path
import sys


def main():
    workspace, stage = Path(sys.argv[1]), sys.argv[2]
    path = workspace / "shared/report.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    expected_title = "September Review" if stage == "s2" else "September Review (Confirmed)"
    checks = [
        ("latest_user_edit", data.get("note") == "User update: keep offline orders", "Preserved latest user note"),
        ("result_correct", data.get("title") == expected_title and data.get("scope") == "All orders",
         "Title and confirmed scope match"),
        ("image_applied", data.get("accent") == "#2463eb", "Applied swatch blue #2463eb"),
    ]
    print(json.dumps({"checks": [{"id": key, "passed": passed, "evidence": evidence}
                                 for key, passed, evidence in checks], "observations": data},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
