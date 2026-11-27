"""Locate, recalculate (LibreOffice copy) and scan the submitted SLA workbook."""
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import openpyxl
from openpyxl.formula import Tokenizer
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.exceptions import InvalidFileException

PRIMARY_REL = "HarborPoint_SLA_Credit/HarborPoint_Oct2026_SLA_Workbook.xlsx"
SEARCH_REL = "HarborPoint_SLA_Credit"

# Exceptions that identify a corrupt / non-workbook submission file.
CORRUPT_ERRORS = (zipfile.BadZipFile, InvalidFileException, KeyError, ValueError,
                  TypeError, IndexError, AttributeError, EOFError)
# Errors that identify a missing or inaccessible submission path.
ACCESS_ERRORS = (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError)

_XCU = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="ODFRecalcMode" oor:op="fuse"><value>0</value></prop></item>
</oor:items>
"""


class RecalcEnvironmentError(RuntimeError):
    """LibreOffice cannot recalculate even the trusted original: not a Worker fault."""


def _is_archive(rel_parts):
    return any("archive" in p.casefold() for p in rel_parts)


def _has_ledger_sheet(names):
    return any("ledger" in re.sub(r"[^a-z]", "", n.casefold()) for n in names)


def locate(workspace):
    """Return (path or None, info dict).  Reads the declared path directly; the
    design-declared fallback search is used only when that path does not exist."""
    ws = Path(workspace)
    primary = ws / PRIMARY_REL
    info = {"declared_path": PRIMARY_REL}
    try:
        with open(primary, "rb") as fh:
            fh.read(4)
        info["used_path"] = PRIMARY_REL
        info["selection"] = "declared path"
        return primary, info
    except FileNotFoundError:
        info["declared_path_status"] = "missing"
    except (IsADirectoryError, PermissionError, NotADirectoryError) as exc:
        info["declared_path_status"] = "not readable: %s" % type(exc).__name__
        info["used_path"] = None
        return None, info
    # Fallback defined by the evaluation design: the non-archive .xlsx under
    # HarborPoint_SLA_Credit/ that has a case-ledger sheet.
    candidates, rejected = [], []
    root = ws / SEARCH_REL
    walk_errors = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=walk_errors.append):
        for fn in filenames:
            if not fn.lower().endswith(".xlsx") or fn.startswith("~$"):
                continue
            p = Path(dirpath) / fn
            rel = p.relative_to(ws)
            if _is_archive(rel.parts[1:]):
                rejected.append("%s (archive)" % rel)
                continue
            try:
                wb = openpyxl.load_workbook(p, read_only=True)
                names = wb.sheetnames
                wb.close()
            except ACCESS_ERRORS + CORRUPT_ERRORS as exc:
                rejected.append("%s (unreadable: %s)" % (rel, type(exc).__name__))
                continue
            if _has_ledger_sheet(names):
                candidates.append(p)
            else:
                rejected.append("%s (no ledger sheet)" % rel)
    info["fallback_candidates"] = [str(p.relative_to(ws)) for p in candidates]
    info["fallback_rejected"] = rejected
    if walk_errors:
        info["fallback_walk_errors"] = [repr(e) for e in walk_errors]
    if len(candidates) == 1:
        info["used_path"] = str(candidates[0].relative_to(ws))
        info["selection"] = "fallback: only non-archive .xlsx with a ledger sheet"
        return candidates[0], info
    info["used_path"] = None
    info["selection"] = ("no fallback candidate" if not candidates
                         else "ambiguous: several fallback candidates")
    return None, info


def _soffice_convert(src, outdir, profile):
    os.makedirs(os.path.join(profile, "user"), exist_ok=True)
    with open(os.path.join(profile, "user", "registrymodifications.xcu"), "w") as fh:
        fh.write(_XCU)
    cmd = ["soffice", "--headless", "--norestore",
           "-env:UserInstallation=" + Path(profile).as_uri(),
           "--convert-to", "xlsx:Calc MS Excel 2007 XML", "--outdir", outdir, src]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=240, text=True)
    except subprocess.TimeoutExpired:
        return None, "LibreOffice timed out"
    out = os.path.join(outdir, Path(src).stem + ".xlsx")
    if proc.returncode != 0 or not os.path.isfile(out):
        return None, "LibreOffice rc=%s stdout=%r stderr=%r" % (
            proc.returncode, proc.stdout[-300:], proc.stderr[-300:])
    return out, None


def recalc_copy(path, trusted_control):
    """Copy the submission to a temp dir, recalculate with LibreOffice and
    return (recalc_path, tmpdir, error).  If the submission cannot be converted,
    the trusted original is converted as a control; a control failure is an
    environment error and is raised."""
    if shutil.which("soffice") is None:
        raise RecalcEnvironmentError("soffice not installed")
    tmp = tempfile.mkdtemp(prefix="hp_sla_ver_")
    src = os.path.join(tmp, "submitted.xlsx")
    shutil.copyfile(path, src)
    outdir = os.path.join(tmp, "out")
    os.makedirs(outdir)
    out, err = _soffice_convert(src, outdir, os.path.join(tmp, "lo_profile"))
    if out:
        return out, tmp, None
    ctl_dir = os.path.join(tmp, "control")
    os.makedirs(ctl_dir)
    ctl_src = os.path.join(ctl_dir, "control.xlsx")
    shutil.copyfile(trusted_control, ctl_src)
    ctl_out, ctl_err = _soffice_convert(ctl_src, os.path.join(ctl_dir), os.path.join(tmp, "lo_profile2"))
    if not ctl_out:
        raise RecalcEnvironmentError("LibreOffice failed on trusted control too: %s" % ctl_err)
    return None, tmp, err


def formula_text(cell):
    v = cell.value
    if cell.data_type != "f" or v is None:
        return None
    if isinstance(v, str):
        return v
    text = getattr(v, "text", None)
    return text if isinstance(text, str) else str(v)


def load_cells(submitted_path, recalc_path):
    """Return ({sheet: {coord: {'formula': str|None, 'value': ...}}}, sheet order, defined names).
    Formulas come from the submitted file; values from the recalculated copy."""
    fwb = openpyxl.load_workbook(submitted_path, data_only=False)
    vwb = openpyxl.load_workbook(recalc_path, data_only=True)
    cells, order = {}, []
    vnames = {n.casefold(): n for n in vwb.sheetnames}
    for ws in fwb.worksheets:
        order.append(ws.title)
        vws = vwb[vnames[ws.title.casefold()]] if ws.title.casefold() in vnames else None
        sheet = {}
        coords = set()
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None:
                    coords.add((c.row, c.column))
        if vws is not None:
            for row in vws.iter_rows():
                for c in row:
                    if c.value is not None:
                        coords.add((c.row, c.column))
        for r, ccol in sorted(coords):
            coord = "%s%d" % (get_column_letter(ccol), r)
            fc = ws.cell(row=r, column=ccol)
            f = formula_text(fc)
            val = vws.cell(row=r, column=ccol).value if vws is not None else None
            if f is None and val is None:
                val = fc.value
            sheet[coord] = {"formula": f, "value": val}
        cells[ws.title] = sheet
    names = []
    for name, dn in fwb.defined_names.items():
        names.append((name, None, dn))
    for ws in fwb.worksheets:
        for name, dn in getattr(ws, "defined_names", {}).items():
            names.append((name, ws.title, dn))
    return cells, order, names


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def find_hits(cells, predicate, formula_only):
    hits = []
    for sheet, sc in cells.items():
        for coord, cv in sc.items():
            if formula_only and cv["formula"] is None:
                continue
            v = cv["value"]
            if is_number(v) and predicate(float(v)):
                hits.append({"cell": "%s!%s" % (sheet, coord),
                             "formula": cv["formula"], "value": v})
    return hits


# ---------------- reference scanning -----------------
_CELL_RE = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")
_COL_RE = re.compile(r"^\$?([A-Za-z]{1,3})$")
_ROW_RE = re.compile(r"^\$?(\d+)$")


def _split_sheet(ref):
    if "!" not in ref:
        return None, ref
    sheet, addr = ref.rsplit("!", 1)
    sheet = sheet.strip()
    if sheet.startswith("'") and sheet.endswith("'"):
        sheet = sheet[1:-1].replace("''", "'")
    return sheet, addr


def _parse_area(addr):
    """Return (c1, r1, c2, r2) with None meaning unbounded, or None if not an address."""
    parts = addr.split(":")
    if len(parts) == 1:
        m = _CELL_RE.match(parts[0])
        if not m:
            return None
        c = column_index_from_string(m.group(1).upper())
        r = int(m.group(2))
        return (c, r, c, r)
    if len(parts) != 2:
        return None
    a, b = parts
    ma, mb = _CELL_RE.match(a), _CELL_RE.match(b)
    if ma and mb:
        c1, c2 = sorted((column_index_from_string(ma.group(1).upper()), column_index_from_string(mb.group(1).upper())))
        r1, r2 = sorted((int(ma.group(2)), int(mb.group(2))))
        return (c1, r1, c2, r2)
    ca, cb = _COL_RE.match(a), _COL_RE.match(b)
    if ca and cb:
        c1, c2 = sorted((column_index_from_string(ca.group(1).upper()), column_index_from_string(cb.group(1).upper())))
        return (c1, None, c2, None)
    ra, rb = _ROW_RE.match(a), _ROW_RE.match(b)
    if ra and rb:
        r1, r2 = sorted((int(ra.group(1)), int(rb.group(1))))
        return (None, r1, None, r2)
    return None


def _area_contains(area, col, row):
    c1, r1, c2, r2 = area
    if c1 is not None and not (c1 <= col <= c2):
        return False
    if r1 is not None and not (r1 <= row <= r2):
        return False
    return True


def _ref_targets(ref, home_sheet):
    """Yield (sheet, area) for an A1-style reference token (with ':' 3-D sheet spans
    treated as both end sheets)."""
    sheet, addr = _split_sheet(ref)
    if sheet is not None and sheet.startswith("["):
        return []  # external workbook
    area = _parse_area(addr)
    if area is None:
        return []
    if sheet is None:
        return [(home_sheet, area)]
    if ":" in sheet:
        return [(s, area) for s in sheet.split(":")]
    return [(sheet, area)]


def _formula_operands(formula):
    try:
        tok = Tokenizer(formula if formula.startswith("=") else "=" + formula)
    except Exception as exc:  # tokenizer rejects malformed formula text
        return None, repr(exc)
    return [t.value for t in tok.items if t.type == "OPERAND" and t.subtype == "RANGE"], None


def references_to(cells, names, target_sheet, coord):
    """Return list of formula cells that reference target_sheet!coord directly,
    through a range containing it, or through a defined name pointing at it."""
    m = _CELL_RE.match(coord)
    col, row = column_index_from_string(m.group(1)), int(m.group(2))
    tkey = target_sheet.casefold()

    def hits_target(ref, home):
        for sh, area in _ref_targets(ref, home):
            if sh.casefold() == tkey and _area_contains(area, col, row):
                return True
        return False

    name_hits = set()
    for name, scope, dn in names:
        try:
            dests = list(dn.destinations)
        except Exception:
            dests = []
        for sh, addr in dests:
            if hits_target("'%s'!%s" % (sh.replace("'", "''"), addr), sh):
                name_hits.add(name.casefold())
    found, tokenize_errors = [], []
    for sheet, sc in cells.items():
        for c, cv in sc.items():
            f = cv["formula"]
            if not f:
                continue
            ops, err = _formula_operands(f)
            if ops is None:
                tokenize_errors.append("%s!%s: %s" % (sheet, c, err))
                if re.search(r"(?<![A-Za-z$])\$?%s\$?%d(?!\d)" % (m.group(1), row), f, re.I):
                    found.append({"cell": "%s!%s" % (sheet, c), "formula": f, "via": "text match (untokenizable)"})
                continue
            for op in ops:
                if hits_target(op, sheet):
                    found.append({"cell": "%s!%s" % (sheet, c), "formula": f, "via": op})
                    break
                if op.casefold() in name_hits:
                    found.append({"cell": "%s!%s" % (sheet, c), "formula": f, "via": "defined name " + op})
                    break
    return found, tokenize_errors


def dump_sheets(cells, order):
    out = {}
    for sheet in order:
        lines = []
        for coord, cv in cells[sheet].items():
            v = cv["value"]
            vs = v.isoformat() if hasattr(v, "isoformat") else repr(v) if isinstance(v, str) else str(v)
            lines.append("%s!%s | %s | %s" % (sheet, coord, cv["formula"] or "", vs))
        out[sheet] = "\n".join(lines)
    return out
