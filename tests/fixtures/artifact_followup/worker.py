"""Deterministic two-stage Worker stub verifying that the User role reads delivered workspace artifacts."""

import csv
import json
from pathlib import Path
import sys

request = json.load(sys.stdin)
root = Path(request["workspace"])
region = "N" if request.get("session_id") else "ALL"
with (root / "materials/orders.csv").open(encoding="utf-8") as stream:
    orders = [row for row in csv.DictReader(stream) if region == "ALL" or row["region"] == region]
result = {"region": region, "count": len(orders), "amount": sum(float(row["amount"]) for row in orders)}
output = root / "output/summary.json"
output.parent.mkdir(exist_ok=True)
output.write_text(json.dumps(result), encoding="utf-8")
print(json.dumps({"text": f"Summarized {region} scope and delivered output/summary.json.",
                  "session_id": "orders"}))
