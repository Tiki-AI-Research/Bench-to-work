"""Header-based binding of the submitted calibration workbook and formula tracing.

Binding never relies on fixed addresses: the Calc result columns, the Dept_Pools
department block and the Company Promotion Pool block are located by their header
and label text.  Every field keeps its own error so one broken column does not
fail unrelated checks.
"""
import re
from openpyxl.formula import Tokenizer
from openpyxl.formula.tokenizer import TokenizerError
from openpyxl.utils.cell import range_boundaries, get_column_letter
from openpyxl.worksheet.formula import ArrayFormula

from . import model

INPUT_SHEETS = {"roster", "assumptions", "bands", "merit_matrix"}
ID_RE = re.compile(r"^E\d{4}$")


def norm(s):
    return re.sub(r"\s+", " ", str(s)).strip().casefold() if s is not None else ""


def num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def r4(v):
    return None if v is None else round(v, 4)


# ---------------------------------------------------------------- column binding
CALC_FIELDS = {
    # field: (exact template header, predicate)
    "id": ("employee id", lambda h: h.startswith("employee id")),
    "dept": ("department", lambda h: "department" in h and "rate" not in h),
    "eff_level": ("effective level", lambda h: "effective level" in h),
    "base": ("base salary (usd)", lambda h: ("base salary" in h or "current base" in h) and "new" not in h),
    "rating": ("rating", lambda h: "rating" in h),
    "months": ("eligible months", lambda h: "eligible months" in h),
    "merit_inc": ("merit increase (usd)", lambda h: "merit increase" in h and "recurring" not in h),
    "promo_inc": ("promotion increase (usd)", lambda h: "promotion increase" in h and "recurring" not in h),
    "rec_promo": ("recurring promotion increase (usd)", lambda h: "recurring" in h and "promotion" in h),
    "rec_merit": ("recurring merit increase (usd)", lambda h: "recurring" in h and "merit" in h),
    "lump": ("lump sum (usd)", lambda h: "lump" in h),
    "new_base": ("new base salary (usd)", lambda h: "new base" in h),
}
# Calc columns that are formulas in the shipped workbook (A-M built, N-Q to complete); a typed
# number in one of these columns on an employee row is a pasted result, not an input layer.
TEMPLATE_CALC_HEADERS = {
    "employee id", "employee name", "department", "current level", "effective level", "base salary (usd)",
    "rating", "eligible months", "proration factor", "merit %", "merit increase (usd)",
    "promotion increase (usd)", "band max (effective level)", "recurring promotion increase (usd)",
    "recurring merit increase (usd)", "lump sum (usd)", "new base salary (usd)"}
SECONDARY_EXCLUDE = ("approv", "flag", "status", "note", "pre-", "pre ", "original", "matrix",
                     "before", "prior", "old", "check", "%", "pct", "percent", "reason")


def bind_columns(headers, fields):
    """headers: {col_index: normalized text}. Returns ({field: col}, {field: error})."""
    cols, errs = {}, {}
    for f, (exact, pred) in fields.items():
        cands = [c for c, h in headers.items() if h and pred(h)]
        pick = None
        if len(cands) == 1:
            pick = cands[0]
        elif len(cands) > 1:
            ex = [c for c in cands if headers[c] == exact]
            if len(ex) == 1:
                pick = ex[0]
            else:
                flt = [c for c in cands if not any(w in headers[c] for w in SECONDARY_EXCLUDE)]
                if len(flt) == 1:
                    pick = flt[0]
        if pick is None:
            errs[f] = ("no column header matches" if not cands else
                       "ambiguous headers: " + "; ".join(f"{get_column_letter(c)}='{headers[c]}'" for c in cands))
        else:
            cols[f] = pick
    return cols, errs


def find_sheet(wb, name, probe):
    for ws in wb.worksheets:
        if norm(ws.title) == name:
            return ws
    for ws in wb.worksheets:
        if probe(ws):
            return ws
    return None


def _row_texts(ws, r, maxc=None):
    out = {}
    for c in range(1, (maxc or ws.max_column) + 1):
        v = ws.cell(r, c).value
        if isinstance(v, str) and v.strip():
            out[c] = norm(v)
    return out


# ---------------------------------------------------------------- Calc
def bind_calc(vwb):
    """Bind the Calc tab of the recalculated workbook."""
    res = {"sheet": None, "header_row": None, "cols": {}, "col_errors": {}, "rows": {},
           "duplicates": [], "error": None}

    def probe(ws):
        return any("employee id" in t for r in range(1, 6) for t in _row_texts(ws, r).values()) and \
            any("recurring" in t for r in range(1, 6) for t in _row_texts(ws, r).values())
    ws = find_sheet(vwb, "calc", probe)
    if ws is None:
        res["error"] = "no Calc sheet (by name or by 'Employee ID'/'Recurring' headers)"
        return res
    res["sheet"] = ws.title
    hr = None
    for r in range(1, 6):
        if any(t.startswith("employee id") for t in _row_texts(ws, r).values()):
            hr = r
            break
    if hr is None:
        res["error"] = f"{ws.title}: no 'Employee ID' header in rows 1-5"
        return res
    res["header_row"] = hr
    headers = _row_texts(ws, hr)
    res["headers"] = {get_column_letter(c): h for c, h in headers.items()}
    cols, errs = bind_columns(headers, CALC_FIELDS)
    res["cols"], res["col_errors"] = cols, errs
    if "id" not in cols:
        res["error"] = "Employee ID column not bound"
        return res
    seen = {}
    for r in range(hr + 1, ws.max_row + 1):
        v = ws.cell(r, cols["id"]).value
        if isinstance(v, str) and ID_RE.match(v.strip()):
            eid = v.strip()
            if eid in seen:
                res["duplicates"].append(eid)
                continue
            seen[eid] = r
    res["rows"] = seen
    res["ws"] = ws
    return res


def calc_value(calc, eid, field):
    """Return (value, error) for a Calc field of an employee from the recalculated workbook."""
    if field not in calc["cols"]:
        return None, f"column '{field}' unbound ({calc['col_errors'].get(field, 'n/a')})"
    r = calc["rows"].get(eid)
    if r is None:
        return None, f"{eid} not found"
    v = calc["ws"].cell(r, calc["cols"][field]).value
    return v, None


def calc_num(calc, eid, field):
    v, e = calc_value(calc, eid, field)
    if e:
        return None, e
    n = num(v)
    if n is None:
        loc = f"{calc['sheet']}!{get_column_letter(calc['cols'][field])}{calc['rows'][eid]}"
        return None, f"{loc} not numeric ({v!r})"
    return n, None


def calc_cell_addr(calc, eid, field):
    if field not in calc["cols"] or eid not in calc["rows"]:
        return None
    return (calc["sheet"], f"{get_column_letter(calc['cols'][field])}{calc['rows'][eid]}")


# ---------------------------------------------------------------- Dept_Pools
POOL_FIELDS = {
    "budget": ("merit pool budget (usd)", lambda h: "budget" in h and "promotion" not in h),
    "spend": ("merit spend — recurring (usd)", lambda h: "spend" in h and "promotion" not in h),
    "remaining": ("remaining (usd)", lambda h: "remaining" in h and not any(
        w in h for w in ("after", "approv", "pending", "ceiling", "unapproved"))),
    "status": ("status / approval", lambda h: "status" in h),
    "overage": ("overage (usd)", lambda h: re.search(r"\bover", h) is not None and not any(
        w in h for w in ("status", "approv", "pending", "ceiling", "remaining", "after"))),
}


def bind_pools(vwb):
    res = {"sheet": None, "header_row": None, "cols": {}, "col_errors": {}, "rows": {},
           "duplicates": [], "error": None, "dept_col": None}

    def find_header(ws):
        for r in range(1, 11):
            t = _row_texts(ws, r)
            dc = [c for c, h in t.items() if h == "department"]
            if dc and any("budget" in h for h in t.values()):
                return r, dc[0], t
        return None

    ws = find_sheet(vwb, "dept_pools", lambda w: find_header(w) is not None)
    if ws is None:
        res["error"] = "no Dept_Pools sheet"
        return res
    res["sheet"] = ws.title
    fh = find_header(ws)
    if fh is None:
        res["error"] = f"{ws.title}: no header row with 'Department' and a budget column"
        return res
    hr, dc, headers = fh
    res["header_row"], res["dept_col"] = hr, dc
    res["headers"] = {get_column_letter(c): h for c, h in headers.items()}
    cols, errs = bind_columns(headers, POOL_FIELDS)
    errs.pop("overage", None)  # optional representation
    res["cols"], res["col_errors"] = cols, errs
    names = {norm(d): d for d in model.DEPTS}
    rows = {}
    for r in range(hr + 1, ws.max_row + 1):
        lab = norm(ws.cell(r, dc).value)
        if "promotion pool" in lab:
            break
        key = None
        if lab in names:
            key = names[lab]
        elif "total" in lab and ("company" in lab or lab == "total"):
            key = "Company Total"
        if key:
            if key in rows:
                res["duplicates"].append(key)
            else:
                rows[key] = r
    res["rows"] = rows
    res["ws"] = ws
    return res


def pool_cell(pools, key, field):
    """(value, address, error) for a department-block cell."""
    if pools.get("error"):
        return None, None, pools["error"]
    if field not in pools["cols"]:
        return None, None, f"column '{field}' unbound ({pools['col_errors'].get(field, 'absent')})"
    if key in pools["duplicates"]:
        return None, None, f"row '{key}' duplicated"
    r = pools["rows"].get(key)
    if r is None:
        return None, None, f"row '{key}' not found"
    c = pools["cols"][field]
    return pools["ws"].cell(r, c).value, (pools["sheet"], f"{get_column_letter(c)}{r}"), None


# ---------------------------------------------------------------- promotion pool block
def bind_promo(vwb, prefer_sheet=None):
    """Locate the Company Promotion Pool block by its label (pools sheet first; a label that is
    only prose, e.g. in README, yields no budget/spend values and is skipped)."""
    res = {"sheet": None, "items": {}, "error": None}
    sheets = sorted(vwb.worksheets, key=lambda w: (norm(w.title) != norm(prefer_sheet or "dept_pools"),))
    tried = []
    for ws in sheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and "company promotion pool" in norm(c.value) and len(c.value) < 80:
                    items = _promo_items(ws, c.row)
                    tried.append(f"{ws.title}!{c.coordinate}")
                    if "budget" in items and "spend" in items:
                        res.update(sheet=ws.title, title_row=c.row, items=items)
                        return res
                    if not res["items"]:
                        res.update(sheet=ws.title, title_row=c.row, items=items)
    if not tried:
        res["error"] = "no 'Company Promotion Pool' block label"
    elif not res["items"].get("budget") or not res["items"].get("spend"):
        missing = [k for k in ("budget", "spend") if k not in res["items"]]
        res["error"] = f"promotion pool block at {', '.join(tried)}: no numeric {'/'.join(missing)} beside its label"
    return res


def _promo_items(ws, r0):
    keys = {"budget": lambda l: "budget" in l, "spend": lambda l: "spend" in l,
            "remaining": lambda l: "remaining" in l,
            "overage": lambda l: re.search(r"\bover", l) is not None and "status" not in l and "approv" not in l,
            "status": lambda l: "status" in l}
    items = {}
    for r in range(r0 + 1, min(ws.max_row, r0 + 25) + 1):
        cells = [(c, ws.cell(r, c).value) for c in range(1, ws.max_column + 1)]
        lab_c = next((c for c, v in cells if isinstance(v, str) and v.strip()), None)
        if lab_c is None:
            continue
        lab = norm(ws.cell(r, lab_c).value)
        if "lump" in lab and "promotion" not in lab:
            break
        for k, pred in keys.items():
            if k in items or not pred(lab):
                continue
            if k == "status":
                val_c = next((c for c, v in cells if c > lab_c and v not in (None, "")), None)
            else:
                val_c = next((c for c, v in cells if c > lab_c and num(v) is not None), None)
            if val_c is not None:
                items[k] = {"label": ws.cell(r, lab_c).value, "value": ws.cell(r, val_c).value,
                            "addr": (ws.title, f"{get_column_letter(val_c)}{r}")}
            break
        if "remaining" in lab and "status" not in items and "remaining" in items:
            # status text written beside the remaining figure
            ra = items["remaining"]["addr"][1]
            rc = range_boundaries(ra)[0]
            txt = next(((c, v) for c, v in cells if c > rc and isinstance(v, str) and v.strip()), None)
            if txt:
                items["status"] = {"label": "(beside remaining)", "value": txt[1],
                                   "addr": (ws.title, f"{get_column_letter(txt[0])}{r}")}
    if "budget" not in items or "spend" not in items:
        # horizontal layout: header row with budget/spend, values on the next row
        for r in range(r0 + 1, min(ws.max_row, r0 + 10) + 1):
            t = _row_texts(ws, r)
            if any("budget" in h for h in t.values()) and any("spend" in h for h in t.values()):
                for c, h in t.items():
                    for k, pred in keys.items():
                        if k not in items and pred(h):
                            v = ws.cell(r + 1, c).value
                            items[k] = {"label": ws.cell(r, c).value, "value": v,
                                        "addr": (ws.title, f"{get_column_letter(c)}{r + 1}")}
                break
    return items


# ---------------------------------------------------------------- status wording
OVER_WORDS = re.compile(r"\bover|exceed|above|deficit|shortfall")
NEG_APPROVAL_RE = re.compile(
    r"\bno\s+(?:[a-z&/]+\s+){0,3}?approv"
    r"|\bapprov\w*\s+not\s+(?:required|needed)"
    r"|\b(?:does|do)\s+not\s+(?:require|need)\s+(?:[a-z&/]+\s+){0,3}?approv"
    r"|\bnot\s+requir\w+\s+(?:[a-z&/]+\s+){0,3}?approv"
    r"|\bwithout\s+(?:[a-z&/]+\s+){0,3}?approv"
)
POS_APPROVAL_RE = re.compile(
    r"\b(?:cfo|cpo|vp|exec(?:utive)?)\s+approv\w*\s+(?:required|needed|pending)"
    r"|\b(?:pending|awaiting)\s+(?:[a-z&/]+\s+){0,3}?approv"
    r"|\b(?:requir(?:es|ing|ed)|need(?:s|ed|ing))\s+(?:[a-z&/]+\s+){0,4}?approv"
    r"|\bapprov\w*\s+(?:required|needed)\b"
)
WITHIN_WORDS = re.compile(
    r"within|\bunder\b|surplus|inside|\bok\b|on budget|in budget"
    r"|\bno\s+(?:[a-z&/]+\s+){0,3}?approv"
    r"|\bapprov\w*\s+not\s+(?:required|needed)"
    r"|not over|\bno\s+(?:[a-z]+\s+)?overage|\b0\s+(?:[a-z]+\s+)?overage|headroom"
)


def status_class(text):
    if not isinstance(text, str) or not text.strip():
        return None
    t = norm(text)
    o = OVER_WORDS.search(t)
    if o is None and not NEG_APPROVAL_RE.search(t):
        o = POS_APPROVAL_RE.search(t)
    w = WITHIN_WORDS.search(t)
    if o and (not w or o.start() < w.start()):
        return "over"
    if w:
        return "within"
    return None


# ---------------------------------------------------------------- formula tracing
class Tracer:
    """Follows cell references in the formula workbook (data_only=False)."""

    def __init__(self, fwb):
        self.wb = fwb
        self.sheets = {ws.title.casefold(): ws for ws in fwb.worksheets}
        self.memo = {}

    def formula(self, sheet, coord):
        ws = self.sheets.get(sheet.casefold())
        if ws is None:
            return None, "missing sheet"
        v = ws[coord].value
        if isinstance(v, ArrayFormula):
            v = v.text
        if isinstance(v, str) and v.startswith("="):
            return v, None
        return None, v

    def _refs(self, sheet, formula):
        try:
            tok = Tokenizer(formula)
        except (TokenizerError, ValueError, IndexError) as e:
            return None, f"unparseable formula: {e}"
        out = []
        for t in tok.items:
            if t.type != "OPERAND" or t.subtype != "RANGE":
                continue
            out.extend(self._resolve(sheet, t.value))
        return out, None

    def _resolve(self, sheet, ref):
        ref = ref.strip()
        if "!" in ref:
            sh, rng = ref.rsplit("!", 1)
            sh = sh.strip("'").replace("''", "'")
            if sh.startswith("["):
                return [("#external", ref)]
        else:
            sh, rng = sheet, ref
        rng = rng.replace("$", "")
        try:
            range_boundaries(rng)
            return [(sh, rng)]
        except (ValueError, TypeError):
            pass
        dn = self.wb.defined_names.get(ref) if hasattr(self.wb.defined_names, "get") else None
        ws = self.sheets.get(sheet.casefold())
        if dn is None and ws is not None and ref in getattr(ws, "defined_names", {}):
            dn = ws.defined_names[ref]
        if dn is not None:
            try:
                return [(s, r.replace("$", "")) for s, r in dn.destinations]
            except (ValueError, TypeError, AttributeError):
                return [("#unresolved", ref)]
        return [("#unresolved", ref)]

    def _cells(self, sh, rng):
        ws = self.sheets.get(sh.casefold())
        if ws is None:
            return []
        c1, r1, c2, r2 = range_boundaries(rng)
        c1 = c1 or 1
        r1 = r1 or 1
        c2 = min(c2 or ws.max_column, ws.max_column)
        r2 = min(r2 or ws.max_row, ws.max_row)
        return [(ws.title, f"{get_column_letter(c)}{r}") for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]

    def closure(self, sheet, coord, limit=20000):
        """All cells reachable from (sheet, coord); input-sheet cells are leaves."""
        seen, stack, problems = set(), [(sheet, coord)], []
        while stack and len(seen) < limit:
            sh, co = stack.pop()
            key = (sh.casefold(), co)
            if key in seen:
                continue
            seen.add(key)
            if sh.casefold() in INPUT_SHEETS and (sh, co) != (sheet, coord):
                continue
            f, _ = self.formula(sh, co)
            if f is None:
                continue
            refs, err = self._refs(sh, f)
            if err:
                problems.append(f"{sh}!{co}: {err}")
                continue
            for rs, rr in refs:
                if rs.startswith("#"):
                    problems.append(f"{sh}!{co}: {rs[1:]} reference {rr}")
                    continue
                if rs.casefold() not in self.sheets:
                    problems.append(f"{sh}!{co}: reference to missing sheet {rs}")
                    continue
                stack.extend(self._cells(rs, rr))
        return seen, problems

    def trace(self, sheet, coord):
        """Return dict(formula, constant, traced, reached_inputs, problems)."""
        key = (sheet.casefold(), coord)
        if key in self.memo:
            return self.memo[key]
        f, const = self.formula(sheet, coord)
        if f is None:
            out = {"formula": None, "constant": const, "traced": False, "inputs": [], "problems": []}
        else:
            seen, probs = self.closure(sheet, coord)
            ins = sorted({s for s, _ in seen if s in INPUT_SHEETS})
            out = {"formula": f, "constant": None, "traced": bool(ins), "inputs": ins, "problems": probs[:5]}
        self.memo[key] = out
        return out


def describe_trace(addr, t):
    s = f"{addr[0]}!{addr[1]}"
    if t["formula"] is None:
        return f"{s} constant {t['constant']!r}"
    if not t["traced"]:
        return f"{s} formula {t['formula'][:60]!r} does not reach Roster/Assumptions/Bands/Merit_Matrix"
    return None
