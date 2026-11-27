"""Submission file candidates and their content facts (stage-work markers, figures, statuses).

Identification is by content only: no modification times, no comparison with the shipped template
as an identity test. Shipped text is excluded only so that it is not mistaken for new content."""
import os
import re
import statistics
from pathlib import Path

from . import model
from .workbook import (bind_calc, bind_pools, bind_promo, calc_num, calc_value, pool_cell, norm, num, r4,
                       INPUT_SHEETS, status_class)
from .xlsx_io import load_formulas, load_cached, recalc_values, SubmissionUnreadable
from .summary import money, MONEY_RE

# Approval Record (Nov 24) content: approvals granted, the CFO ceiling/condition, the CPO 5,982.60,
# the VP's three named lump sums, or a reference to the record itself.
APPROVAL_MARKERS = [
    ("approved together with an approver", re.compile(
        r"\b(?:cfo|cpo|vp|klein|vasquez|whitmore)\b[^.;\n]{0,80}\bapproved\b|\bapproved\b[^.;\n]{0,80}"
        r"\b(?:cfo|cpo|vp|klein|vasquez|whitmore)\b", re.I)),
    ("CFO ceiling / condition", re.compile(
        r"\bcfo\b[^.\n]{0,80}(?:2,?500|ceiling|sum of (?:the )?department)|(?:2,?500|ceiling)[^.\n]{0,80}\bcfo\b",
        re.I)),
    ("CPO 5,982.60 approved", re.compile(r"5,?982\.60?[^.;\n]{0,80}\bapproved\b|\bapproved\b[^.;\n]{0,80}5,?982\.60?",
                                         re.I)),
    ("reference to the Nov 24 Approval Record", re.compile(r"approval record|nov(?:ember)?\.? 24|11/24|2026-11-24", re.I)),
]
TITLE_LINE = re.compile(r"^\s*(to|from|cc|re|date)\s*:", re.I)


def walk_files(workspace, suffix):
    out = []

    def onerr(e):
        raise e
    for root, dirs, files in os.walk(workspace, onerror=onerr):
        dirs[:] = sorted(x for x in dirs if not x.startswith("."))
        for f in sorted(files):
            if f.lower().endswith(suffix) and not f.startswith(("~$", ".~lock")):
                out.append(str((Path(root) / f).relative_to(workspace)))
    return out


def approval_markers(texts):
    hits = []
    for t in texts:
        if not isinstance(t, str):
            continue
        lines = [ln for ln in t.splitlines() if not TITLE_LINE.match(ln)]
        txt = "\n".join(lines)
        for lab, rx in APPROVAL_MARKERS:
            if rx.search(txt):
                hits.append(f"{lab}: {txt[:120]!r}")
                break
    return hits


def effective_factors(calc, ids):
    """Per-employee merit factor (computed merit / matrix merit) from the workbook's own columns."""
    per, errs = {}, []
    for eid in ids:
        vals = {f: calc_num(calc, eid, f) for f in ("base", "rating", "months", "rec_promo", "rec_merit", "lump")}
        e = [x for _, x in vals.values() if x]
        if e:
            errs += e
            continue
        v = {k: x for k, (x, _) in vals.items()}
        pi, pe = calc_num(calc, eid, "promo_inc")
        if pe is None:
            computed = v["rec_merit"] + v["lump"] - max(0.0, pi - v["rec_promo"])
            src = "recurring merit + lump sum - (promotion increase - recurring promotion)"
        else:
            mi, me = calc_num(calc, eid, "merit_inc")
            if me:
                errs.append(f"{eid}: neither promotion-increase nor merit-increase column usable ({pe}; {me})")
                continue
            computed, src = mi, "merit increase column"
        rate = model.MERIT_MATRIX.get(int(v["rating"])) if float(v["rating"]).is_integer() else None
        if rate is None:
            errs.append(f"{eid} rating {v['rating']!r} not in Merit_Matrix")
            continue
        mm = v["base"] * rate * v["months"] / 12.0
        per[eid] = {"recurring_merit": v["rec_merit"], "lump": v["lump"], "computed_merit": computed,
                    "matrix_merit": mm, "factor": computed / mm if mm else None, "source": src}
    return per, errs


class Workbook:
    """One .xlsx candidate: formulas, stored cache, LibreOffice recalculation and bindings."""

    def __init__(self, workspace, rel, template_cells):
        self.rel = rel
        self.path = Path(workspace) / rel
        self.error = None
        try:
            self.fwb = load_formulas(self.path)
            self.cwb = load_cached(self.path)
        except SubmissionUnreadable as e:
            self.error = str(e)
            return
        self.vwb = recalc_values(self.path)
        self.calc = bind_calc(self.vwb)
        self.pools = bind_pools(self.vwb)
        self.promo = bind_promo(self.vwb)
        self._tpl = template_cells
        self.facts = self._facts()

    def _cv(self, eid, f):
        if self.calc.get("error"):
            return f"<{self.calc['error']}>"
        v, e = calc_value(self.calc, eid, f)
        return (r4(v) if isinstance(v, float) else v) if e is None else f"<{e}>"

    def _pool(self, key, f):
        v, a, e = pool_cell(self.pools, key, f)
        return ((r4(v) if isinstance(v, float) else v) if e is None else f"<{e}>"), ("!".join(a) if a else None)

    def _facts(self):
        F = {"name": self.rel}
        F["E2007_rating"] = self._cv("E2007", "rating")
        F["E1009_department"] = self._cv("E1009", "dept")
        F["E1012_effective_level"] = self._cv("E1012", "eff_level")
        F["E1012_promotion_increase"] = self._cv("E1012", "promo_inc")
        F["E4002_eligible_months"] = self._cv("E4002", "months")
        ok = not self.calc.get("error")
        prod = [e for e in self.calc.get("rows", {}) if ok and norm(calc_value(self.calc, e, "dept")[0]) == "product"]
        F["calc_product_rows"] = sorted(prod)
        sums = {}
        for lab, ids in (("product", prod), ("company", list(self.calc.get("rows", {})))):
            tot, bad = 0.0, False
            for e in ids:
                v, err = calc_num(self.calc, e, "rec_merit")
                if err:
                    bad = True
                    break
                tot += v
            sums[lab] = None if (bad or not ok) else tot
        F["calc_product_recurring_merit_sum"] = r4(sums["product"]) if sums["product"] is not None else None
        F["calc_company_recurring_merit_sum"] = r4(sums["company"]) if sums["company"] is not None else None
        self.calc_sums = sums
        F["dept_pools_product_spend"], _ = self._pool("Product", "spend")
        F["dept_pools_company_spend"], _ = self._pool("Company Total", "spend")
        F["dept_pools_company_budget"], _ = self._pool("Company Total", "budget")
        st = {}
        for key in model.DEPTS + ["Company Total"]:
            v, a = self._pool(key, "status")
            st[key] = {"value": v, "cell": a}
        if not self.promo.get("error") and "status" in self.promo["items"]:
            it = self.promo["items"]["status"]
            st["Company Promotion Pool"] = {"value": it["value"], "cell": "!".join(it["addr"])}
        F["stored_status_cells"] = st
        per, errs = effective_factors(self.calc, prod) if ok else ({}, [self.calc.get("error")])
        fs = [p["factor"] for p in per.values() if p["factor"] is not None]
        F["product_effective_factor_median"] = statistics.median(fs) if fs else None
        F["product_factor_range"] = [min(fs), max(fs)] if fs else None
        F["factor_errors"] = errs[:5]
        F["batch_markers"] = self.batch_markers()
        F["approval_markers"] = self.approval_markers()
        return F

    # ---------------------------------------------------------------- stage-work markers
    def batch_markers(self):
        if self.calc.get("error"):
            return []
        out = []
        d, e = calc_value(self.calc, "E1009", "dept")
        if e is None and norm(d) == "product":
            out.append("E1009 in Product")
        lv, e1 = calc_value(self.calc, "E1012", "eff_level")
        pi, e2 = calc_num(self.calc, "E1012", "promo_inc")
        if e1 is None and norm(lv) == "l4" and e2 is None and pi > 0:
            out.append("E1012 at L4 with promotion amount")
        m, e = calc_num(self.calc, "E4002", "months")
        if e is None and abs(m - 12) < 1e-9:
            out.append("E4002 at 12 eligible months")
        return out

    def status_area_texts(self):
        """Text cells (recalculated) of the Dept_Pools sheet, the promotion block and approval-type tabs,
        excluding text identical to the shipped workbook at the same address."""
        out = []
        for w in self.vwb.worksheets:
            t = w.title.casefold()
            if t in INPUT_SHEETS or (not self.calc.get("error") and t == self.calc["sheet"].casefold()):
                continue
            if not (t == (self.pools.get("sheet") or "").casefold() or
                    any(k in t for k in ("approv", "register", "status", "exception", "decision", "pool"))):
                continue
            for row in w.iter_rows():
                for c in row:
                    if isinstance(c.value, str) and c.value.strip():
                        if self._tpl.get((t, c.coordinate)) == c.value:
                            continue
                        out.append(c.value)
        return out

    def approval_markers(self):
        return approval_markers(self.status_area_texts())


def template_cells(code_dir):
    """Text cells of the trusted shipped workbook (trusted material: errors propagate)."""
    wb = load_formulas(Path(code_dir) / model.TRUSTED_WORKBOOK)
    return {(w.title.casefold(), c.coordinate): c.value for w in wb.worksheets for row in w.iter_rows()
            for c in row if isinstance(c.value, str)}


# ---------------------------------------------------------------- summary markers / figures
def summary_batch_markers(info):
    out = []
    for u in info.get("content_units", []):
        if any(abs(float(m.group(3).replace(",", "")) - 808.0) < 0.005 for m in MONEY_RE.finditer(u)):
            out.append(f"808 lump sum: {u[:100]!r}")
            break
    rows = (info.get("dept_table") or {}).get("rows", {})
    for key, want in (("Product", "over"), ("Engineering", "within")):
        r = rows.get(key)
        if not r:
            continue
        cls = status_class(r.get("status"))
        rem = money(r.get("remaining") or "")
        sign = None if rem is None else ("over" if rem < 0 else "within")
        if cls == want or (cls is None and sign == want):
            out.append(f"{key} shown {want} its pool: {r}")
    return out


def summary_approval_markers(info):
    return approval_markers(info.get("content_units", []))


def summary_figures(info):
    """Product and company recurring merit spend as shown in the summary's department table."""
    rows = (info.get("dept_table") or {}).get("rows", {})
    out = {}
    for key in ("Product", "Company Total"):
        out[key] = money((rows.get(key) or {}).get("spend") or "")
    return out


def figures_match(sfig, wfacts, tol=1.0):
    """True/False when the summary's table figures agree with a workbook (whole-dollar display allowed);
    None when the summary carries no comparable figure."""
    pairs = [(sfig.get("Product"), wfacts.get("dept_pools_product_spend")),
             (sfig.get("Company Total"), wfacts.get("dept_pools_company_spend"))]
    pairs = [(a, num(b)) for a, b in pairs if a is not None]
    if not pairs:
        return None
    return all(b is not None and abs(a - b) <= tol for a, b in pairs)
