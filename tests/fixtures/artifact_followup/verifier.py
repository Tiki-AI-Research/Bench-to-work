"""Independently compute expected stage totals from raw orders without giving answers to the User role."""

import csv
import json
from pathlib import Path
import sys

workspace, stage_id = Path(sys.argv[1]), sys.argv[2]
region = "ALL" if stage_id == "s1" else "N"
with (Path(__file__).parent / "materials/orders.csv").open(encoding="utf-8") as stream:
    orders = [row for row in csv.DictReader(stream) if region == "ALL" or row["region"] == region]
expected = {"region": region, "count": len(orders), "amount": sum(float(row["amount"]) for row in orders)}
try:
    actual = json.loads((workspace / "output/summary.json").read_text(encoding="utf-8"))
except (FileNotFoundError, json.JSONDecodeError):
    actual = None
print(json.dumps({"checks": [{"id": "correct_report", "passed": actual == expected,
                             "evidence": f"Submitted: {actual}; expected from raw orders: {expected}"}], "observations": {}}))
