"""Workbook loading and LibreOffice recalculation for submitted workbooks.

The submitted file is never modified: it is copied into a private temp dir and
LibreOffice converts the copy with "always recalculate on load" forced through a
throw-away user profile, so cached values reflect the formulas, not stale caches.
"""
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException

_XCU = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="ODFRecalcMode" oor:op="fuse"><value>0</value></prop></item>
</oor:items>
"""

# Exceptions that identify a damaged / non-workbook submission (not a system fault).
CORRUPT_XLSX = (zipfile.BadZipFile, InvalidFileException, KeyError, ValueError, TypeError,
                IndexError, AttributeError)


class SubmissionUnreadable(Exception):
    """The submitted file is missing, not accessible, or not a usable workbook/document."""


def load_formulas(path):
    try:
        return openpyxl.load_workbook(path, data_only=False)
    except FileNotFoundError as e:
        raise SubmissionUnreadable(f"missing: {path.name}") from e
    except (IsADirectoryError, PermissionError) as e:
        raise SubmissionUnreadable(f"not accessible: {path.name}: {e}") from e
    except CORRUPT_XLSX as e:
        raise SubmissionUnreadable(f"corrupt workbook {path.name}: {type(e).__name__}: {e}") from e


def load_cached(path):
    """Cached values exactly as stored in the submitted file (no recalculation)."""
    try:
        return openpyxl.load_workbook(path, data_only=True)
    except FileNotFoundError as e:
        raise SubmissionUnreadable(f"missing: {path.name}") from e
    except (IsADirectoryError, PermissionError) as e:
        raise SubmissionUnreadable(f"not accessible: {path.name}: {e}") from e
    except CORRUPT_XLSX as e:
        raise SubmissionUnreadable(f"corrupt workbook {path.name}: {type(e).__name__}: {e}") from e


def recalc_values(path, timeout=240):
    """Return an openpyxl data_only workbook of a LibreOffice-recalculated copy.

    soffice missing or failing on a file that openpyxl could open is a runtime
    fault and is raised, not converted into a Worker failure.
    """
    soffice = shutil.which("soffice")
    if soffice is None:
        raise RuntimeError("soffice not available for recalculation")
    with tempfile.TemporaryDirectory(prefix="ver_recalc_") as td:
        td = Path(td)
        prof = td / "profile"
        (prof / "user").mkdir(parents=True)
        (prof / "user" / "registrymodifications.xcu").write_text(_XCU, encoding="utf-8")
        src = td / "in" / "book.xlsx"
        src.parent.mkdir()
        shutil.copyfile(path, src)
        out = td / "out"
        out.mkdir()
        cmd = [soffice, "--headless", "--norestore", f"-env:UserInstallation=file://{prof}",
               "--convert-to", "xlsx:Calc MS Excel 2007 XML", "--outdir", str(out), str(src)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        res = out / "book.xlsx"
        if not res.is_file():
            raise RuntimeError(f"LibreOffice recalculation produced no output (rc={proc.returncode}): "
                               f"{proc.stderr.strip()[:300]}")
        return openpyxl.load_workbook(res, data_only=True)
