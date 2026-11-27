"""Task-specific Excel verifier: inspects workbook formulas and perturbs inputs in a scratch copy.

The Runner invokes this script as an external command and reads JSON from stdout without hardcoding
spreadsheet domain rules. Missing or inaccurate deliverables yield passed=false; missing host tools
such as LibreOffice raise a non-zero exit code so infrastructure errors never masquerade as 0 scores.
"""

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

ROOT = Path(__file__).resolve().parent
HEADERS = ["OrderID", "Region", "Quantity", "UnitPrice", "Refund", "NetAmount"]
REGIONS = ["East", "South", "North"]
CHECKS = ["source_intact", "rows_correct", "formulas_work", "totals_correct", "readable"]


def expected_rows(stage: str) -> dict:
    """Derive expected rows directly from raw source materials rather than reference workbooks."""
    wb = load_workbook(ROOT / "materials/sales_source.xlsx", data_only=True)
    rows = {r[0]: list(r[1:5]) for r in wb["Orders"].iter_rows(min_row=2, values_only=True) if r[0]}
    wb.close()
    if stage in {"s2", "s3"}:
        with (ROOT / "materials/missing_orders.csv").open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                item = [r["Region"], *[float(r[k]) for k in HEADERS[2:5]]]
                if r["OrderID"] in rows and rows[r["OrderID"]] != item:
                    raise ValueError("Conflicting duplicate order in task materials")
                rows[r["OrderID"]] = item
    if stage == "s3":
        with (ROOT / "materials/corrections.csv").open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                i = HEADERS.index(r["Field"]) - 1
                rows[r["OrderID"]][i] = r["NewValue"] if i == 0 else float(r["NewValue"])
    return rows


def totals(rows: dict) -> dict:
    result = {r: 0 for r in REGIONS}
    for region, qty, price, refund in rows.values():
        result[region] += qty * price - refund
    result["Total"] = sum(result.values())
    return result


def close(actual, expected) -> bool:
    return isinstance(actual, (float, int)) and not isinstance(actual, bool) and math.isclose(
        actual, expected, abs_tol=0.005, rel_tol=0)


def recalculate(path: Path, directory: Path):
    """Run headless LibreOffice on a temporary copy without mutating the submitted artifact."""
    executable = shutil.which("libreoffice")
    if not executable:
        raise RuntimeError("LibreOffice is required for formula verification; install it and rerun")
    directory.mkdir()
    profile = directory / "profile"
    runtime_dir = directory / "runtime"
    cache_dir = directory / "cache"
    config_dir = directory / "config"
    runtime_dir.mkdir(mode=0o700)
    cache_dir.mkdir()
    config_dir.mkdir()
    lo_env = {
        **os.environ,
        "HOME": str(directory),
        "XDG_RUNTIME_DIR": str(runtime_dir),
        "XDG_CACHE_HOME": str(cache_dir),
        "XDG_CONFIG_HOME": str(config_dir),
        "GSETTINGS_BACKEND": "memory",
        "SAL_USE_VCLPLUGIN": "svp",
    }
    result = subprocess.run([executable, f"-env:UserInstallation={profile.as_uri()}",
                             "--headless", "--convert-to", "xlsx", "--outdir", str(directory),
                             str(path)], env=lo_env, capture_output=True, text=True, timeout=30, encoding="utf-8")
    output = directory / path.name
    if result.returncode or not output.is_file():
        raise RuntimeError(f"Excel recalculation failed: {result.stderr or result.stdout}")
    return load_workbook(output, data_only=True)


def values_match(wb, expected: dict) -> bool:
    detail = {r[0]: r[5] for r in wb["Orders"].iter_rows(min_row=2, max_col=6, values_only=True) if r[0]}
    summary = {r[0]: r[1] for r in wb["Summary"].iter_rows(min_row=2, max_col=2, values_only=True) if r[0]}
    return (set(detail) == set(expected) and set(summary) == set(totals(expected))
            and all(close(detail[k], r[1] * r[2] - r[3]) for k, r in expected.items())
            and all(close(summary[k], v) for k, v in totals(expected).items()))


def verify(workspace: Path, stage: str) -> dict:
    expected = expected_rows(stage)
    findings = {cid: {"id": cid, "passed": False, "evidence": "No inspectable workbook submitted"} for cid in CHECKS}
    observations = {"expected_order_count": len(expected), "expected_totals": totals(expected)}

    def record(cid, passed, evidence):
        findings[cid] = {"id": cid, "passed": bool(passed), "evidence": evidence}

    names = ["sales_source.xlsx"] + (["missing_orders.csv"] if stage != "s1" else []) + (["corrections.csv"] if stage == "s3" else [])
    intact = all((workspace / "materials" / n).is_file() and
                 hashlib.sha256((workspace / "materials" / n).read_bytes()).digest() ==
                 hashlib.sha256((ROOT / "materials" / n).read_bytes()).digest() for n in names)
    record("source_intact", intact, "SHA-256 check on published materials: " + ("intact" if intact else "missing or modified"))
    path = workspace / "output/sales_report.xlsx"
    if not path.is_file():
        observations["artifact"] = "Missing output/sales_report.xlsx"
        return {"checks": list(findings.values()), "observations": observations}
    try:
        wb = load_workbook(path, data_only=False)
        if set(wb.sheetnames) != {"Orders", "Summary"}:
            raise ValueError("Workbook sheets must be Orders and Summary")
        sheet = wb["Orders"]
        if [c.value for c in sheet[1]][:6] != HEADERS:
            raise ValueError("Orders header mismatch")
        if [c.value for c in wb["Summary"][1]][:2] != ["Region", "NetAmount"]:
            raise ValueError("Summary header mismatch")
        populated = [r for r in sheet.iter_rows(min_row=2, max_col=6) if any(c.value is not None for c in r)]
        actual = {r[0].value: [c.value for c in r[1:5]] for r in populated}
        rows_ok = len(populated) == len(expected) and actual == expected
        record("rows_correct", rows_ok, f"Found {len(populated)} rows; expected {len(expected)} unique orders; " +
               ("all fields match" if rows_ok else f"actual rows: {actual}"))
        observations["actual_rows"] = actual
        summaries = [r for r in wb["Summary"].iter_rows(min_row=2, max_col=2) if any(c.value is not None for c in r)]
        formula_cells = [r[5] for r in populated] + [r[1] for r in summaries]
        formulas = (len(summaries) == 4 and len({r[0].value for r in summaries}) == 4
                    and all(c.data_type == "f" for c in formula_cells))
        readable = (sheet.freeze_panes == "A2" and
                    all("0.00" in c.number_format for r in populated for c in r[3:6]))
        record("readable", readable, "Checked frozen top row in Orders and two-decimal number formats")

        with tempfile.TemporaryDirectory(prefix="excel-verifier-") as tmp:
            temp = Path(tmp)
            original = temp / "original.xlsx"
            shutil.copy2(path, original)
            calculated = recalculate(original, temp / "calculated")
            values_ok = values_match(calculated, expected)
            observations["actual_totals"] = {r[0]: r[1] for r in calculated["Summary"].iter_rows(
                min_row=2, max_col=2, values_only=True) if r[0]}
            calculated.close()
            record("totals_correct", values_ok, f"LibreOffice recalculated totals: {observations['actual_totals']}; "
                   f"expected: {totals(expected)}; per-order net amounts also verified")

            dynamic_ok = False
            if rows_ok and formulas:
                # Perturb the last order's region, quantity, price, and refund to verify formula range coverage.
                row = populated[-1][0].row
                oid = sheet.cell(row, 1).value
                changed = {k: list(v) for k, v in expected.items()}
                changed[oid] = ["South" if expected[oid][0] == "East" else "East",
                                expected[oid][1] + 1, expected[oid][2] + 7, expected[oid][3] + 3]
                for col, value in enumerate(changed[oid], start=2):
                    sheet.cell(row, col, value)
                probe = temp / "probe.xlsx"
                wb.save(probe)
                perturbed = recalculate(probe, temp / "perturbed")
                dynamic_ok = values_match(perturbed, changed)
                perturbed.close()
            record("formulas_work", formulas and dynamic_ok,
                   f"Formula cells present: {formulas}; recalculated after perturbing last row: {dynamic_ok}")
        wb.close()
    except (BadZipFile, InvalidFileException, KeyError, ValueError, TypeError) as exc:
        observations["artifact_error"] = str(exc)
        for cid in CHECKS[1:]:
            record(cid, False, f"Workbook could not be read under task schema: {exc}")
    return {"checks": list(findings.values()), "observations": observations}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--stage", choices=["s1", "s2", "s3"], required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.workspace.resolve(), args.stage), ensure_ascii=False))
