"""Verify that the spreadsheet verifier catches formula, deduplication, and range mutations."""

import importlib.util
from pathlib import Path
import shutil

from openpyxl import load_workbook
import pytest

ROOT = Path(__file__).resolve().parent / "fixtures/excel_revision"
spec = importlib.util.spec_from_file_location("excel_verifier", ROOT / "verifier.py")
verifier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verifier)


def workspace(tmp_path, stage):
    shutil.copytree(ROOT / "materials", tmp_path / "materials")
    (tmp_path / "output").mkdir()
    path = tmp_path / "output/sales_report.xlsx"
    shutil.copy2(ROOT / f"reference/{stage}.xlsx", path)
    return path


@pytest.mark.parametrize("stage", ["s1", "s2", "s3"])
def test_reference_recalculates_and_responds_to_input(tmp_path, stage):
    workspace(tmp_path, stage)
    result = verifier.verify(tmp_path, stage)
    assert all(c["passed"] for c in result["checks"]), result


@pytest.mark.parametrize("stage,mutation,failed", [
    ("s2", lambda w: w["Orders"].append(["O103", "South", 3, 80, 0, "=C9*D9-E9"]), "rows_correct"),
    ("s3", lambda w: w["Orders"].delete_rows(8), "rows_correct"),
    ("s3", lambda w: setattr(w["Orders"]["C2"], "value", 999), "rows_correct"),
    ("s2", lambda w: setattr(w["Summary"]["B2"], "value", "=SUMIF(Orders!B2:B6,A2,Orders!F2:F6)"), "totals_correct"),
    ("s3", lambda w: setattr(w["Summary"]["B2"], "value", "=800"), "formulas_work"),
    ("s3", lambda w: setattr(w["Orders"]["F2"], "value", 200), "formulas_work"),
])
def test_mutations_are_detected(tmp_path, stage, mutation, failed):
    path = workspace(tmp_path, stage)
    book = load_workbook(path)
    mutation(book)
    book.save(path)
    book.close()
    result = verifier.verify(tmp_path, stage)
    assert not next(c["passed"] for c in result["checks"] if c["id"] == failed), result


def test_invalid_workbook_is_artifact_failure(tmp_path):
    path = workspace(tmp_path, "s1")
    path.write_text("not an Excel file", encoding="utf-8")
    checks = {c["id"]: c["passed"] for c in verifier.verify(tmp_path, "s1")["checks"]}
    assert not checks["rows_correct"]
    assert checks["source_intact"]


def test_missing_engine_is_infrastructure_error(tmp_path, monkeypatch):
    workspace(tmp_path, "s1")
    monkeypatch.setattr(verifier.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="LibreOffice"):
        verifier.verify(tmp_path, "s1")
