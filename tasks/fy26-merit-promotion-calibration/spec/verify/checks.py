"""Stage checks for the FY26 calibration task. Each check judges only its own evidence."""
import re
import statistics
from pathlib import Path

from openpyxl.utils.cell import get_column_letter, coordinate_from_string, column_index_from_string

from . import model
from .workbook import (bind_calc, bind_pools, bind_promo, calc_num, calc_value, calc_cell_addr,
                       pool_cell, num, norm, r4, status_class, OVER_WORDS, Tracer, describe_trace,
                       TEMPLATE_CALC_HEADERS)
from .xlsx_io import load_formulas, SubmissionUnreadable
from .summary import (list_docx, looks_like_summary, parse_summary, summary_overview, template_texts,
                      TEMPLATE_PLACEHOLDERS)
from .candidates import (walk_files, Workbook, template_cells, effective_factors, summary_batch_markers,
                         summary_approval_markers, summary_figures, figures_match)

TOL = 0.05 + 1e-9
ROUND_TOL = 0.50 + 1e-9
ORIGINAL_WB = model.TRUSTED_WORKBOOK
ORIGINAL_SUMMARY = model.TRUSTED_SUMMARY
RESULT_FIELDS = ["rec_promo", "rec_merit", "lump", "new_base"]
FIELD_LABEL = {"rec_promo": "recurring promotion", "rec_merit": "recurring merit", "lump": "lump sum",
               "new_base": "new base", "months": "eligible months", "base": "base salary",
               "rating": "rating", "dept": "department", "eff_level": "effective level",
               "promo_inc": "promotion increase", "merit_inc": "merit increase"}
REF_KEY = {"rec_promo": "rec_promo", "rec_merit": "rec_merit", "lump": "lump", "new_base": "new_base",
           "months": "eligible_months", "base": "base", "rating": "rating"}
STAGE_ISO = {"s4_final_package": "2026-12-02", "s5_cfo_product_decision": "2026-12-08"}
SHIPPED_OTHER_DOCX = ("FY26_Calibration_Approval_Record_2026-11-24.docx", "FY26_Product_Merit_Decision_2026-12-07.docx")
BATCH_IDS = ["E1009", "E1012", "E4002", "E3001"]


def fmt(v):
    return "None" if v is None else (f"{v:,.2f}" if isinstance(v, float) else repr(v))


def short(items, n=8):
    items = list(items)
    s = "; ".join(items[:n])
    return s + (f"; ... (+{len(items) - n} more)" if len(items) > n else "")


def result(cid, passed, evidence):
    return {"id": cid, "passed": bool(passed), "evidence": evidence}


def near(a, b, tol=TOL):
    return a is not None and b is not None and abs(a - b) <= tol


class Ctx:
    """Per-run context. The delivered workbook (and at s4/s5 the delivered summary) is selected once by
    content and shared by every check of the stage."""

    def __init__(self, workspace, stage, code_dir):
        self.ws = Path(workspace)
        self.code_dir = Path(code_dir)
        self.stage = stage
        self.ref = model.reference(code_dir, stage)
        self.obs = {"stage": stage}
        self._wb_loaded = False
        self._summaries = None
        self._docs = None
        self._wbs = {}
        self._tpl_cells = None
        self._pair = None

    # ------------------------------------------------------------ candidates
    def wb_candidates(self):
        if not hasattr(self, "_wbc"):
            self._wbc = walk_files(self.ws, ".xlsx")
        return self._wbc

    def wb(self, rel):
        if rel not in self._wbs:
            if self._tpl_cells is None:
                self._tpl_cells = template_cells(self.code_dir)
            self._wbs[rel] = Workbook(self.ws, rel, self._tpl_cells)
        return self._wbs[rel]

    def docs(self):
        """Every .docx in the submission except the shipped reference documents, parsed."""
        if self._docs is None:
            tpl = template_texts(self.code_dir)
            self._docs = [parse_summary(p, self.ws, tpl) for p in list_docx(self.ws, SHIPPED_OTHER_DOCX)]
        return self._docs

    def summaries(self):
        if self._summaries is None:
            self._summaries = [i for i in self.docs() if i["error"] or looks_like_summary(i)]
            self.obs["summary_files"] = [summary_overview(i) for i in self._summaries]
        return self._summaries

    # ------------------------------------------------------------ selection
    def stage_pair(self):
        """s4/s5: (summary info or None, summary reason, workbook rel or None, workbook reason)."""
        if self._pair is not None:
            return self._pair
        iso = STAGE_ISO[self.stage]
        sums = [i for i in self.summaries() if not i["error"] and not i["draft_marking"]]
        S = [i for i in sums if iso in i.get("issue_dates", [])]
        names = self.wb_candidates()
        facts = {}
        for rel in names:
            w = self.wb(rel)
            facts[rel] = None if w.error else w.facts
        W = [rel for rel in names if facts[rel] is not None and self._carries_stage_work(facts[rel])]
        sel = {"issue_date": iso,
               "summary_candidates": [{"path": i["path"], "date_line": i["date_line"], "issue_dates": i.get("issue_dates"),
                                       "draft_marking": i["draft_marking"], "name_draft": i["name_draft"],
                                       "placeholders": i["placeholders"], "figures": summary_figures(i),
                                       "qualifies": i in S} for i in self.summaries() if not i["error"]],
               "workbook_candidates": [dict(facts[r] or {"name": r, "error": self.wb(r).error},
                                            carries_stage_work=r in W) for r in names],
               "rule": "content only: summary = non-DRAFT .docx carrying the issue date in title block/header/date "
                       "line; workbook = .xlsx carrying the stage's work; several -> the one matching the "
                       "selected summary's figures; none -> original name"}
        self.obs["delivered_selection"] = sel
        # ---- summary
        summ, sw = None, None
        contents = {}
        for i in S:
            contents.setdefault(_docx_text(i), []).append(i)
        ref_w = [facts[r] for r in W] or ([facts[ORIGINAL_WB]] if facts.get(ORIGINAL_WB) else [])
        if not S:
            sw = (f"no non-DRAFT summary carries the {iso} issue date in its title block, header or date line "
                  f"(non-DRAFT summaries: {[(i['path'], i['issue_dates']) for i in sums] or 'none'})")
        elif len(contents) == 1:
            summ = _canonical(S, lambda i: i["path"], ORIGINAL_SUMMARY)
            sw = (f"{summ['path']} is the only non-DRAFT summary dated {iso}" if len(S) == 1 else
                  f"{len(S)} non-DRAFT summaries dated {iso} with identical content; {summ['path']} read")
        else:
            m = [i for i in S if any(figures_match(summary_figures(i), f) for f in ref_w)]
            mc = {_docx_text(i) for i in m}
            if len(mc) == 1:
                summ = _canonical(m, lambda i: i["path"], ORIGINAL_SUMMARY)
                sw = (f"{len(S)} conflicting summaries dated {iso}; {summ['path']} is the one whose department "
                      f"table matches the workbook")
            else:
                sw = (f"ambiguous: {len(S)} non-DRAFT summaries dated {iso} with conflicting content "
                      f"({', '.join(i['path'] for i in S)}) and the workbook does not resolve them "
                      f"({len(m)} match its figures)")
        # ---- workbook
        uncertain = False
        if len(W) == 1:
            wrel, ww = W[0], f"{W[0]} is the only workbook carrying the stage's work"
        elif W:
            m = [r for r in W if summ is not None and figures_match(summary_figures(summ), facts[r])]
            if len(m) == 1:
                wrel, ww = m[0], f"{len(W)} workbooks carry the stage's work; {m[0]} matches the selected summary"
            else:
                wrel = _canonical(m or W, lambda r: r, ORIGINAL_WB)
                uncertain = True
                ww = (f"{len(W)} workbooks carry the stage's work ({', '.join(W)}) and the summary does not single "
                      f"one out; {wrel} read (original name first, else first by path) - see coverage limit")
        else:
            wrel, ww = ORIGINAL_WB, "no workbook carries the stage's work; original name read"
        sel.update(selected_summary=summ["path"] if summ else None, summary_rule=sw, selected_workbook=wrel,
                   workbook_rule=ww, workbook_selection_uncertain=uncertain)
        self._pair = (summ, sw, wrel, ww)
        return self._pair

    def _carries_stage_work(self, f):
        if self.stage == "s4_final_package":
            return num(f.get("E2007_rating")) == 4
        x = f.get("product_effective_factor_median")
        rng = f.get("product_factor_range")
        return x is not None and rng is not None and (abs(rng[0] - 1) > 1e-6 or abs(rng[1] - 1) > 1e-6)

    def select_workbook(self):
        if self.stage in STAGE_ISO:
            _, _, rel, why = self.stage_pair()
            return rel, why
        names = self.wb_candidates()
        if ORIGINAL_WB in names:
            return ORIGINAL_WB, "original file name"
        others = [r for r in names if not self.wb(r).error]
        if len(others) == 1:
            return others[0], f"{ORIGINAL_WB} absent; the only readable .xlsx"
        if not others:
            return ORIGINAL_WB, "original file name (no other readable .xlsx)"
        if self.stage == "s1_complete_baseline":
            q = [r for r in others if not self.wb(r).calc.get("error") and self.wb(r).calc_sums["company"] is not None]
        else:
            q = [r for r in others if self.wb(r).facts["batch_markers"]]
        if len(q) == 1:
            return q[0], f"{ORIGINAL_WB} absent; the only .xlsx carrying this stage's work"
        return ORIGINAL_WB, f"{ORIGINAL_WB} absent and {len(others)} other workbooks cannot be told apart by content"

    def load_workbook(self):
        if self._wb_loaded:
            return
        self._wb_loaded = True
        self.wb_error = self.fwb = self.vwb = self.cwb = None
        name, how = self.select_workbook()
        self.obs["workbook_selection"] = {"selected": name, "rule": how, "candidates": list(self.wb_candidates())}
        self.wb_path = self.ws / name
        w = self.wb(name)
        if w.error:
            self.wb_error = w.error
            self.obs["workbook_selection"]["error"] = w.error
            return
        self.cur = w
        self.fwb, self.cwb, self.vwb = w.fwb, w.cwb, w.vwb
        self.calc, self.pools, self.promo = w.calc, w.pools, w.promo
        self.tracer = Tracer(self.fwb)
        self.obs["workbook_binding"] = {
            "file": name,
            "recalculated_with": "LibreOffice headless on a temp copy, recalc-on-load forced",
            "calc": {k: self.calc.get(k) for k in ("sheet", "header_row", "error", "duplicates")} |
            {"columns": {f: get_column_letter(c) for f, c in self.calc.get("cols", {}).items()},
             "column_errors": self.calc.get("col_errors"), "employee_rows": len(self.calc.get("rows", {}))},
            "dept_pools": {k: self.pools.get(k) for k in ("sheet", "header_row", "error", "duplicates", "rows")} |
            {"columns": {f: get_column_letter(c) for f, c in self.pools.get("cols", {}).items()},
             "column_errors": self.pools.get("col_errors")},
            "promotion_pool": {"sheet": self.promo.get("sheet"), "error": self.promo.get("error"),
                               "items": {k: {"label": v["label"], "addr": "!".join(v["addr"])}
                                         for k, v in self.promo.get("items", {}).items()}},
        }

    def wb_ok(self):
        self.load_workbook()
        if self.wb_error:
            return False, f"workbook {self.wb_path.name} unusable: {self.wb_error}"
        if self.calc.get("error"):
            return False, f"Calc not bound: {self.calc['error']}"
        return True, None


def _canonical(items, key, original):
    """Deterministic pick among equivalent candidates: the original name if present, else first by path."""
    for i in items:
        if Path(key(i)).name == original and Path(key(i)).parent == Path("."):
            return i
    return sorted(items, key=key)[0]


# ================================================================ helpers
def coverage(ctx, ids=None):
    want = set(ids if ids is not None else ctx.ref["order"])
    have = set(ctx.calc["rows"])
    errs = []
    if want - have:
        errs.append("missing IDs " + ",".join(sorted(want - have)))
    if ids is None and have - set(ctx.ref["order"]):
        errs.append("unexpected IDs " + ",".join(sorted(have - set(ctx.ref["order"]))))
    dup = sorted(set(ctx.calc["duplicates"]) & (want if ids is not None else set(ctx.calc["duplicates"])))
    if dup:
        errs.append("duplicate IDs " + ",".join(dup))
    return errs


def compare_emps(ctx, ids, fields, tol=TOL):
    bad, n = [], 0
    for eid in ids:
        exp_row = ctx.ref["employees"][eid]
        for f in fields:
            exp = exp_row[REF_KEY[f]]
            act, err = calc_num(ctx.calc, eid, f)
            n += 1
            if err:
                bad.append(f"{eid} {FIELD_LABEL[f]}: {err}")
            elif abs(act - float(exp)) > tol:
                bad.append(f"{eid} {FIELD_LABEL[f]} {fmt(act)} vs expected {fmt(float(exp))}")
    return bad, n


def calc_sum(ctx, field, ids=None):
    ids = ids if ids is not None else ctx.ref["order"]
    tot, errs = 0.0, []
    for eid in ids:
        v, e = calc_num(ctx.calc, eid, field)
        if e:
            errs.append(e)
        else:
            tot += v
    return (None if errs else tot), errs


COMPANY_OVER_CLAIM_RE = re.compile(
    r"\b(?:over|exceed\w*|above)\s+(?:the\s+)?(?:company|total|overall|aggregate)\b"
    r"|\b(?:company|total|overall|aggregate)\s+(?:merit\s+|pool\s+|spend\s+|budget\s+){0,4}?"
    r"(?:is\s+)?(?:over\b|exceed\w*|above\b|deficit|shortfall)"
)


def _company_total_dept_overage_note_ok(ctx, sv, got):
    """When Company Total remaining >= 0 while >= 1 individual department is over its pool, accept a
    Company Total status cell that notes department-level overages / CFO approval required without
    claiming total company spend exceeds total company budget."""
    if not isinstance(sv, str) or not sv.strip():
        return False
    over_depts = [d for d in model.DEPTS if ctx.ref["dept"][d]["over"]]
    if not over_depts:
        return False
    t = norm(sv)
    if COMPANY_OVER_CLAIM_RE.search(t):
        return False
    has_dept_scope = bool(re.search(r"\b(?:dept|department)s?\b", t)) or any(norm(d) in t for d in over_depts)
    if has_dept_scope and (got == "over" or "approv" in t):
        return True
    if not OVER_WORDS.search(t) and got == "over":
        return True
    return False


def eval_dept(ctx, key, need_status=True, optional_status=False, tol=TOL):
    """Budget / spend / remaining (or overage) / status of a Dept_Pools row vs reference."""
    ref = ctx.ref["dept"][key]
    msgs, act, ok = [], {}, True
    for f in ("budget", "spend"):
        v, addr, err = pool_cell(ctx.pools, key, f)
        act[f] = v
        if err:
            ok = False
            msgs.append(f"{key} {f}: {err}")
        elif num(v) is None:
            ok = False
            msgs.append(f"{key} {f} {'!'.join(addr)} not numeric ({v!r})")
        elif not near(num(v), ref[f], tol):
            ok = False
            msgs.append(f"{key} {f} {fmt(num(v))} vs {fmt(ref[f])}")
    rv, raddr, rerr = pool_cell(ctx.pools, key, "remaining")
    ov, oaddr, oerr = pool_cell(ctx.pools, key, "overage")
    exp_rem = ref["remaining"]
    rem_ok = num(rv) is not None and near(num(rv), exp_rem, tol)
    if not rem_ok and not oerr and num(ov) is not None:
        o = num(ov)
        over_ok = near(o, -exp_rem, tol) if exp_rem < 0 else (near(o, 0.0, tol) or near(o, -exp_rem, tol))
        rem_cell_ok = rv in (None, "") or (num(rv) is not None and near(num(rv), max(0.0, exp_rem), tol))
        rem_ok = over_ok and rem_cell_ok
    if not rem_ok:
        ok = False
        msgs.append(f"{key} remaining {fmt(num(rv)) if num(rv) is not None else repr(rv)}"
                    f"{'' if oerr else ' / overage ' + repr(ov)} vs remaining {fmt(exp_rem)}"
                    + (f" ({rerr})" if rerr else ""))
    if need_status:
        sv, saddr, serr = pool_cell(ctx.pools, key, "status")
        want = "over" if exp_rem < 0 else "within"
        if optional_status and (serr or sv in (None, "")):
            msgs.append(f"{key} status blank/absent (not required)")
        elif serr:
            ok = False
            msgs.append(f"{key} status: {serr}")
        else:
            got = status_class(sv)
            if got != want and not (key == "Company Total" and want == "within"
                                    and _company_total_dept_overage_note_ok(ctx, sv, got)):
                ok = False
                msgs.append(f"{key} status {sv!r} reads as {got} but remaining is {fmt(exp_rem)} ({want})")
    return ok, msgs, act


def eval_promo(ctx, need_status=False, tol=TOL):
    ref = ctx.ref["promo"]
    if ctx.promo.get("error"):
        return False, [ctx.promo["error"]]
    it = ctx.promo["items"]
    ok, msgs = True, []
    for f in ("budget", "spend"):
        v = it.get(f, {}).get("value")
        if f not in it:
            ok = False
            msgs.append(f"promotion pool {f} not found")
        elif not near(num(v), ref[f], tol):
            ok = False
            msgs.append(f"promotion pool {f} {v!r} vs {fmt(ref[f])}")
    rv = it.get("remaining", {}).get("value")
    ov = it.get("overage", {}).get("value")
    rem_ok = near(num(rv), ref["remaining"], tol)
    if not rem_ok and num(ov) is not None and ref["remaining"] < 0:
        rem_ok = near(num(ov), -ref["remaining"], tol) and (rv is None or near(num(rv), 0.0, tol))
    if not rem_ok:
        ok = False
        msgs.append(f"promotion pool remaining {rv!r} (overage {ov!r}) vs {fmt(ref['remaining'])}")
    sv = it.get("status", {}).get("value")
    want = "over" if ref["remaining"] < 0 else "within"
    got = status_class(sv)
    if need_status and got != want:
        ok = False
        msgs.append(f"promotion pool status {sv!r} reads as {got}; expected {want}")
    elif not need_status and got is not None and got != want:
        ok = False
        msgs.append(f"promotion pool status {sv!r} reads as {got}; expected {want}")
    return ok, msgs


def pools_ok(ctx):
    if ctx.pools.get("error"):
        return False, f"Dept_Pools not bound: {ctx.pools['error']}"
    if ctx.pools["duplicates"]:
        return False, "Dept_Pools duplicated rows " + ",".join(ctx.pools["duplicates"])
    return True, None


# ================================================================ liveness
def _calc_template_cols(ctx):
    """Column letters of the formula workbook's Calc sheet whose header is a shipped Calc header."""
    ws = ctx.tracer.sheets.get(ctx.calc["sheet"].casefold())
    hr = ctx.calc["header_row"]
    cols = set()
    for c in range(1, ws.max_column + 1):
        if norm(ws.cell(hr, c).value) in TEMPLATE_CALC_HEADERS:
            cols.add(get_column_letter(c))
    return cols


def _employee_rows(ctx, sheet_cf):
    """Rows of a (non-input) sheet whose recalculated values contain a roster employee ID."""
    cache = ctx.__dict__.setdefault("_emp_rows", {})
    if sheet_cf not in cache:
        ids = set(ctx.ref["order"])
        ws = next((w for w in ctx.vwb.worksheets if w.title.casefold() == sheet_cf), None)
        rows = set()
        if ws is not None:
            for r in ws.iter_rows():
                if any(isinstance(c.value, str) and c.value.strip() in ids for c in r):
                    rows.add(r[0].row)
        cache[sheet_cf] = rows
    return cache[sheet_cf]


def _pasted_in(ctx, closure_cells):
    """Typed numbers reachable from a result cell that stand in for calculated amounts: any typed number in a
    shipped Calc formula column on an employee row, and any typed dollar-sized number (|v| > 100) in a helper
    column / extra tab on an employee row. Input sheets (Roster, Assumptions, Bands, Merit_Matrix) are leaves."""
    from .workbook import INPUT_SHEETS
    calc_sheet = ctx.calc["sheet"].casefold()
    if not hasattr(ctx, "_tcols"):
        ctx._tcols = _calc_template_cols(ctx)
    calc_rows = set(ctx.calc["rows"].values())
    out = []
    for sh, co in closure_cells:
        if sh in INPUT_SHEETS:
            continue
        col, row = coordinate_from_string(co)
        rows = calc_rows if sh == calc_sheet else _employee_rows(ctx, sh)
        if row not in rows:
            continue
        ws = ctx.tracer.sheets.get(sh)
        f, const = ctx.tracer.formula(ws.title, co)
        x = num(const) if f is None else None
        if x is None:
            continue
        if (sh == calc_sheet and col in ctx._tcols) or abs(x) > 100:
            out.append(f"{ws.title}!{co}={const!r}")
    return out


def _cached_vs_recalc(ctx, addr):
    """('ok'|'uncached'|'stale', detail) comparing the file's stored cache with the recalculation."""
    sh, co = addr
    try:
        cv = ctx.cwb[sh][co].value
        rv = ctx.vwb[sh][co].value
    except KeyError:
        return "stale", f"{sh}!{co} sheet missing after recalculation"
    if cv is None:
        return ("ok", None) if rv in (None, "") else ("uncached", None)
    if num(cv) is not None or num(rv) is not None:
        if num(cv) is None or num(rv) is None or abs(num(cv) - num(rv)) > 0.005:
            return "stale", f"{sh}!{co} cached {cv!r} vs recalculated {rv!r}"
        return "ok", None
    if norm(cv) != norm(rv):
        return "stale", f"{sh}!{co} cached {cv!r} vs recalculated {rv!r}"
    return "ok", None


def scan_cells(ctx, include_status):
    """(label, addr, kind) of the result and pool cells that must be live formulas."""
    cells, missing = [], []
    for eid in ctx.ref["order"]:
        for f in RESULT_FIELDS:
            a = calc_cell_addr(ctx.calc, eid, f)
            if a is None:
                missing.append(f"{eid} {FIELD_LABEL[f]}: cell not bound")
            else:
                cells.append((f"{eid} {FIELD_LABEL[f]}", a, "amount"))
    po, perr = pools_ok(ctx)
    if po:
        for key in model.DEPTS + ["Company Total"]:
            for f in ("budget", "spend", "remaining", "overage", "status"):
                if f == "status" and not include_status:
                    continue
                v, addr, err = pool_cell(ctx.pools, key, f)
                if err:
                    if f in ("budget", "spend") or (f == "remaining" and "overage" not in ctx.pools["cols"]):
                        missing.append(f"{key} {f}: {err}")
                    continue
                if v in (None, "") and (f in ("overage", "status") or "overage" in ctx.pools["cols"]
                                        or key == "Company Total" and f == "status"):
                    continue
                cells.append((f"{key} {f}", addr, "status" if f == "status" else "pool"))
    else:
        missing.append(perr)
    if not ctx.promo.get("error"):
        for f in ("budget", "spend", "remaining", "overage", "status"):
            item = ctx.promo["items"].get(f)
            if f == "status" and not include_status:
                continue
            if item is None:
                if f in ("budget", "spend") or (f == "remaining" and "overage" not in ctx.promo["items"]):
                    missing.append(f"promotion pool {f} not found")
                continue
            if f == "status" and ctx.tracer.formula(*item["addr"])[0] is None:
                # Promotion pool status is not part of the shipped template; only trace if written as a formula.
                continue
            cells.append((f"promotion pool {f}", item["addr"], "status" if f == "status" else "pool"))
    else:
        missing.append(ctx.promo["error"])
    return cells, missing


def live_core(ctx, include_status, pool_formulas=True):
    """Formula / trace / pasted-chain / cache checks over the stage's result and pool ranges.
    pool_formulas=False (stage 5): pool-block cells are only compared cache-vs-recalculation; whether they
    report what the per-employee calculation produces is judged by s5_chk_product_target."""
    cells, missing = scan_cells(ctx, include_status)
    if not pool_formulas:
        missing = [m for m in missing if re.match(r"E\d{4} ", m)]
    probs = list(missing)
    pasted, stale, uncached = set(), [], 0
    n_traced = 0
    dept_pool_cells = set()
    if not ctx.pools.get("error"):
        for d in model.DEPTS + ["Company Total"]:
            for pf in ("budget", "spend", "remaining", "overage"):
                _, paddr, perr = pool_cell(ctx.pools, d, pf)
                if not perr and paddr is not None:
                    dept_pool_cells.add((paddr[0].casefold(), paddr[1]))
    for label, addr, kind in cells:
        st, det = _cached_vs_recalc(ctx, addr)
        if st == "stale":
            stale.append(det)
        elif st == "uncached":
            uncached += 1
        if kind == "pool" and not pool_formulas:
            continue
        n_traced += 1
        t = ctx.tracer.trace(*addr)
        d = describe_trace(addr, t)
        if d:
            probs.append(f"{label} {d}")
            continue
        seen, _ = ctx.tracer.closure(*addr)
        pasted.update(_pasted_in(ctx, seen))
        if kind == "status":
            if label.startswith("promotion"):
                promo_figs = {
                    (ctx.promo["items"][x]["addr"][0].casefold(), ctx.promo["items"][x]["addr"][1])
                    for x in ("budget", "remaining", "spend", "overage") if x in ctx.promo["items"]
                }
                dep = [k for k in seen if k in promo_figs]
            else:
                row = int(coordinate_from_string(addr[1])[1])
                dep = [(s, c) for s, c in seen if s == addr[0].casefold() and c != addr[1]
                       and int(coordinate_from_string(c)[1]) == row]
                if not dep and label.startswith("Company Total"):
                    dep = [k for k in seen if k in dept_pool_cells]
                elif not dep:
                    dept_name = label[:-len(" status")] if label.endswith(" status") else ""
                    own_figs = set()
                    if dept_name in model.DEPTS and not ctx.pools.get("error"):
                        for pf in ("budget", "spend", "remaining", "overage"):
                            _, paddr, perr = pool_cell(ctx.pools, dept_name, pf)
                            if not perr and paddr is not None:
                                own_figs.add((paddr[0].casefold(), paddr[1]))
                    dep = [k for k in seen if k in own_figs]
            if not dep:
                probs.append(f"{label} {addr[0]}!{addr[1]} formula does not depend on the pool's own figures")
    return {"cells": len(cells), "traced_cells": n_traced, "problems": probs, "pasted": sorted(pasted),
            "stale": stale, "uncached": uncached}


def live_result(ctx, cid, core, extra_msgs=(), extra_ok=True, scope=""):
    ok = not core["problems"] and not core["pasted"] and not core["stale"] and extra_ok
    ev = (f"openpyxl data_only=False formula/trace over {core.get('traced_cells', core['cells'])} {scope} cells "
          f"(cache comparison over {core['cells']}): formula/trace problems "
          f"{short(core['problems']) or 'none'}; typed amounts standing in for calculations reached by these formulas "
          f"(any typed number in a shipped Calc formula column, or a typed dollar-sized number in a helper cell on an "
          f"employee row): {short(core['pasted'], 5) or 'none'}; stored cache vs LibreOffice recalculation of the "
          f"same file: {len(core['stale'])} differ {short(core['stale'], 4)}"
          + (f", {core['uncached']} formula cells carry no stored cache (not counted as stale)" if core["uncached"] else "")
          + (f"; {short(list(extra_msgs), 8)}" if extra_msgs else "") + "; amounts vs Guidelines not judged here")
    ctx.obs.setdefault("liveness", {})[cid] = {k: (v if not isinstance(v, list) else v[:40]) for k, v in core.items()}
    return result(cid, ok, ev)


def s1_live(ctx, cid):
    ok, why = ctx.wb_ok()
    if not ok:
        return result(cid, False, why)
    core = live_core(ctx, include_status=True)
    return live_result(ctx, cid, core, scope="result/pool (incl. over/within status)")


def s2_live(ctx, cid):
    ok, why = ctx.wb_ok()
    if not ok:
        return result(cid, False, why)
    core = live_core(ctx, include_status=False)
    extra = []
    po, _ = pools_ok(ctx)
    pool_seen = set()
    if po:
        for d in model.DEPTS:
            for f in ("budget", "spend"):
                v, addr, err = pool_cell(ctx.pools, d, f)
                if not err:
                    pool_seen |= ctx.tracer.closure(*addr)[0]
    for eid in BATCH_IDS:
        res_seen = set(pool_seen)
        for f in RESULT_FIELDS:
            a = calc_cell_addr(ctx.calc, eid, f)
            if a is not None:
                res_seen |= ctx.tracer.closure(*a)[0]
        for f in ("dept", "eff_level", "months"):
            a = calc_cell_addr(ctx.calc, eid, f)
            if a is None:
                extra.append(f"{eid} {FIELD_LABEL[f]}: cell not bound")
                continue
            fm, const = ctx.tracer.formula(*a)
            if fm is None:
                extra.append(f"{eid} {FIELD_LABEL[f]} {a[0]}!{a[1]} typed over the Calc formula ({const!r})")
            elif (a[0].casefold(), a[1]) not in res_seen:
                extra.append(f"{eid} {FIELD_LABEL[f]} {a[0]}!{a[1]} does not feed the employee's results or the pools")
    return live_result(ctx, cid, core, ["batch rows' department / effective level / eligible-month cells: "
                                        + (short(extra) or "formulas feeding the chain (Roster edit or "
                                           "adjustment layer)")], not extra, scope="result/pool dollar")


def _roster_cell(ctx, eid, header_word):
    ws = ctx.tracer.sheets.get("roster")
    if ws is None:
        return None
    col = next((c for c in range(1, ws.max_column + 1) if header_word in norm(ws.cell(1, c).value)), None)
    idc = next((c for c in range(1, ws.max_column + 1) if norm(ws.cell(1, c).value) == "employee id"), None)
    if col is None or idc is None:
        return None
    for r in range(2, ws.max_row + 1):
        if str(ws.cell(r, idc).value).strip() == eid:
            return ("roster", f"{get_column_letter(col)}{r}")
    return None


def s4_live(ctx, cid):
    ok, why = ctx.wb_ok()
    if not ok:
        return result(cid, False, why)
    core = live_core(ctx, include_status=False)
    extra, eok = [], True
    a = calc_cell_addr(ctx.calc, "E2007", "rec_merit")
    if a is None:
        eok = False
        extra.append("E2007 recurring merit cell not bound")
    else:
        seen, _ = ctx.tracer.closure(*a)
        rc = calc_cell_addr(ctx.calc, "E2007", "rating")
        rr = _roster_cell(ctx, "E2007", "rating")
        hit = [x for x in ((rc[0].casefold(), rc[1]) if rc else None, rr) if x and x in seen]
        if hit:
            extra.append(f"E2007 merit {'!'.join(a)} traces to {', '.join('!'.join(h) for h in hit)}")
        else:
            eok = False
            extra.append(f"E2007 merit {'!'.join(a)} does not trace to her rating input (Calc rating cell or "
                         f"Roster Performance Rating)")
        po, perr = pools_ok(ctx)
        key = (a[0].casefold(), a[1])
        for d in ("Product", "Company Total"):
            v, addr, err = pool_cell(ctx.pools, d, "spend") if po else (None, None, perr)
            if err:
                eok = False
                extra.append(f"{d} spend: {err}")
            elif key not in ctx.tracer.closure(*addr)[0]:
                eok = False
                extra.append(f"{d} spend {'!'.join(addr)} does not depend on E2007 merit {'!'.join(a)}")
            else:
                extra.append(f"{d} spend {'!'.join(addr)} depends on it")
    return live_result(ctx, cid, core, extra, eok, scope="result/pool dollar")


# ================================================================ product (s5)
def product_analysis(ctx):
    """Effective per-employee factor of Product merit from the workbook's own columns (cached on ctx)."""
    if hasattr(ctx, "_prod"):
        return ctx._prod
    R = ctx.ref["employees"]
    ref_members = [e for e in ctx.ref["order"] if R[e]["dept"] == "Product"]
    out = {"ref_members": ref_members, "errors": [], "target": ctx.ref["product_target"],
           "exact_factor": ctx.ref["product_factor"]}
    members = []
    for eid in ctx.ref["order"]:
        v, e = calc_value(ctx.calc, eid, "dept")
        if e:
            out["errors"].append(f"{eid} department: {e}")
        elif norm(v) == "product":
            members.append(eid)
    out["members"] = members
    per, ferrs = effective_factors(ctx.calc, ref_members)
    out["errors"] += ferrs
    fs = [p["factor"] for p in per.values() if p["factor"] is not None]
    out["per_employee"] = per
    f_med = statistics.median(fs) if fs else None
    # factor cell: a precedent of the Product merit cells holding f, 1-f or matrix% x f
    hits = {}
    if f_med is not None:
        vs = {w.title.casefold(): w for w in ctx.vwb.worksheets}
        bud = None
        if not ctx.pools.get("error"):
            _, bud, _ = pool_cell(ctx.pools, "Product", "budget")
        for eid in ref_members:
            a = calc_cell_addr(ctx.calc, eid, "rec_merit")
            if a is None:
                continue
            seen, _ = ctx.tracer.closure(*a)
            rate = model.MERIT_MATRIX.get(R[eid]["rating"])
            found = []
            for sh, co in sorted(seen):
                w = vs.get(sh)
                x = num(w[co].value) if w is not None else None
                if x is None:
                    continue
                if abs(x - f_med) <= 1e-6:
                    found.append((f"{w.title}!{co}", x, "factor"))
                elif abs(x - (1 - f_med)) <= 1e-6 and abs(f_med - 1) > 1e-9:
                    found.append((f"{w.title}!{co}", x, "reduction"))
                elif rate and abs(x - rate * f_med) <= 1e-8 and abs(f_med - 1) > 1e-9:
                    found.append((f"{w.title}!{co}", x, "adjusted merit %"))
            if not found and bud and (bud[0].casefold(), bud[1]) in seen:
                found.append(("!".join(bud), None, "closed form via Product budget"))
            hits[eid] = found
    out["factor_hits"] = {e: [h[0] + (f"={h[1]!r}" if h[1] is not None else "") + f" ({h[2]})" for h in v][:4]
                          for e, v in hits.items()}
    direct = sorted({h[1] for v in hits.values() for h in v if h[2] == "factor"})
    reduc = sorted({h[1] for v in hits.values() for h in v if h[2] == "reduction"})
    if len(direct) == 1:
        f_wb, f_src = direct[0], "factor cell"
    elif not direct and len(reduc) == 1:
        f_wb, f_src = 1 - reduc[0], "reduction cell (1 - value)"
    else:
        f_wb, f_src = f_med, "median of per-employee effective factors"
    out["f_wb"], out["f_source"] = f_wb, f_src
    out["uniform_bad"] = ([] if f_wb is None else
                          [f"{e} computed merit {fmt(p['computed_merit'])} vs {fmt(f_wb * p['matrix_merit'])} at "
                           f"factor {f_wb:.10f}" for e, p in per.items()
                           if abs(p["computed_merit"] - f_wb * p["matrix_merit"]) > TOL])
    sm, serr = calc_sum(ctx, "rec_merit", ref_members)
    out["calc_spend"], out["calc_spend_errors"] = sm, serr
    out["residual"] = None if sm is None else sm - out["target"]
    cents = [calc_num(ctx.calc, e, "rec_merit")[0] for e in ref_members]
    out["residual_after_per_employee_cent_rounding"] = (
        None if any(c is None for c in cents) else round(sum(round(c, 2) for c in cents) - out["target"], 4))
    # reported pool figures vs the workbook's own per-employee sums
    out["calc_product_rows_sum"] = ctx.cur.calc_sums["product"]
    out["calc_company_sum"] = ctx.cur.calc_sums["company"]
    rep = {}
    for key in ("Product", "Company Total"):
        v, addr, err = pool_cell(ctx.pools, key, "spend") if not ctx.pools.get("error") else (None, None, ctx.pools["error"])
        rep[key] = {"value": v, "cell": "!".join(addr) if addr else None, "error": err}
    out["reported_spend"] = rep
    tol, why = TOL, "standard $0.05"
    if f_wb is not None and abs(f_wb - 1) > 1e-9:
        rec = model.reference(ctx.code_dir, ctx.stage, product_factor=f_wb)
        out["recomputed_at_workbook_factor"] = {"product_spend": rec["dept"]["Product"]["spend"],
                                                "E2006_lump": rec["employees"]["E2006"]["lump"]}
        rounded = any(abs(f_wb - round(out["exact_factor"], k)) <= 1e-9 for k in range(2, 11))
        reproduces = sm is not None and abs(sm - rec["dept"]["Product"]["spend"]) <= TOL
        if rounded and reproduces and not out["uniform_bad"] and abs(f_wb - out["exact_factor"]) > 1e-12:
            tol, why = ROUND_TOL, (f"$0.50: workbook factor {f_wb!r} is the exact factor rounded and recomputing "
                                   f"at it reproduces the workbook spend")
        else:
            why += (f" (rounding allowance not applicable: rounded-exact={rounded}, reproduces={reproduces}, "
                    f"uniform={not out['uniform_bad']})")
    out["tol"], out["tol_reason"] = tol, why
    notes = []
    for w in ctx.fwb.worksheets:
        for row in w.iter_rows():
            for c in row:
                if isinstance(c.value, str) and not c.value.startswith("=") and "round" in norm(c.value):
                    notes.append(f"{w.title}!{c.coordinate}: {c.value[:160]}")
    out["rounding_text_cells"] = notes[:10]
    ctx._prod = out
    return out


def s5_live(ctx, cid):
    ok, why = ctx.wb_ok()
    if not ok:
        return result(cid, False, why)
    core = live_core(ctx, include_status=False, pool_formulas=False)
    pa = product_analysis(ctx)
    extra, eok = [], True
    if pa["f_wb"] is None:
        eok = False
        extra.append("factor not derivable: " + short(pa["errors"], 3))
    elif abs(pa["f_wb"] - 1) <= 1e-9:
        eok = False
        extra.append("no Product reduction factor in the workbook (effective factor 1)")
    else:
        miss = [e for e in pa["ref_members"] if not pa["factor_hits"].get(e)]
        if miss:
            eok = False
            extra.append("Product merit cells not referencing a factor / closed form: " + ",".join(miss))
        else:
            extra.append(f"factor {pa['f_wb']!r} ({pa['f_source']}) referenced by all {len(pa['ref_members'])} "
                         f"Product merit cells, e.g. {short(sorted({h for v in pa['factor_hits'].values() for h in v}), 3)}")
        # lump sum and new base of Product rows derive from the factored merit
        for eid in pa["ref_members"]:
            m = calc_cell_addr(ctx.calc, eid, "rec_merit")
            for f in ("lump", "new_base"):
                a = calc_cell_addr(ctx.calc, eid, f)
                if a is None or m is None:
                    continue
                seen, _ = ctx.tracer.closure(*a)
                hit_cells = {tuple(h.split("=")[0].split(" (")[0].split("!")) for h in pa["factor_hits"].get(eid, [])}
                hit_keys = {(s.casefold(), c) for s, c in hit_cells if c}
                if (m[0].casefold(), m[1]) not in seen and not (hit_keys & seen):
                    eok = False
                    extra.append(f"{eid} {FIELD_LABEL[f]} {'!'.join(a)} does not derive from the factored merit")
    return live_result(ctx, cid, core, extra, eok, scope="per-employee result")


# ================================================================ draft / identity
def _final_presented(i):
    return (not i["draft_marking"] and not i["name_draft"]) or i["name_final"]


def _docx_text(i):
    secs = (i.get("sections") or {}).get("sections", [])
    return repr([(s["heading"], s["paragraphs"], s["tables"], s["placeholders"]) for s in secs]) + repr(
        i.get("title_block")) + repr(i.get("header_footer_text"))


def _is_original(rel, original):
    return Path(rel) == Path(original)


def _carry_decision(kind, original, orig_markers, orig_error, copies):
    """copies: [(rel, markers)]. Returns (ok, message)."""
    carriers = [r for r, m in copies if m]
    if orig_error:
        return False, f"original-name {kind} {original}: {orig_error}"
    if orig_markers:
        return True, (f"{original} carries the stage's work ({short(orig_markers, 3)})"
                      + (f"; other copies with markers: {', '.join(carriers)} (allowed)" if carriers else ""))
    if carriers:
        return False, (f"{original} carries none of the stage's markers while renamed/other-name "
                       f"{'copy' if len(carriers) == 1 else 'copies'} {', '.join(carriers)} "
                       f"{'does' if len(carriers) == 1 else 'do'}: the work is carried outside the original name")
    return True, f"no {kind} carries the stage's markers (no renaming; missing work is for the business checks)"


def _is_untouched_summary_shell(info):
    if info is None or info.get("error"):
        return False
    sfig = summary_figures(info)
    if any(v is not None for v in sfig.values()):
        return False
    return len(info.get("placeholders") or []) >= len(TEMPLATE_PLACEHOLDERS)


def check_draft_hold(ctx, cid):
    infos = ctx.summaries()
    readable = [i for i in infos if not i["error"]]
    finals = [i for i in readable if _final_presented(i)] + [i for i in infos if i["error"] and not i["name_draft"]]
    parts = [f"summary .docx files: {[i['path'] for i in infos] or 'none'}"]
    for i in infos:
        parts.append(f"{i['path']}: unreadable ({i['error']})" if i["error"] else
                     f"{i['path']}: name_draft={i['name_draft']}, DRAFT marking={[t[:50] for t in i['draft_marking']] or 'none'}")
    if finals:
        parts.append("presented as final: " + ", ".join(i["path"] for i in finals))
    if ctx.stage == "s1_complete_baseline":
        drafts = [i for i in readable if i["draft_marking"]]
        stripped = [i for i in readable if not i["draft_marking"] and not _is_untouched_summary_shell(i)]
        if not drafts:
            parts.append("no summary carries a DRAFT marking in its title block/header/core title")
        if stripped:
            parts.append("DRAFT marking stripped from: " + ", ".join(i["path"] for i in stripped))
        return result(cid, bool(drafts) and not finals and not stripped, " | ".join(parts))
    # s2 / s3: either the original-name files or a single clearly DRAFT-marked file carried forward from s1
    s3 = ctx.stage == "s3_approvals"
    mark_doc = summary_approval_markers if s3 else summary_batch_markers
    ok = not finals
    ident = {"marker_basis": ("Approval Record status content (approved + approver, CFO ceiling/condition, CPO "
                              "5,982.60 approved, reference to the Nov 24 record)" if s3 else
                              "post-batch content (workbook: E1009 in Product / E1012 at L4 with promotion / "
                              "E4002 12 months; summary: 808 lump sum / Product over / Engineering within)"),
             "summaries": [], "workbooks": []}
    all_docs = ctx.docs()
    orig = next((i for i in all_docs if _is_original(i["path"], ORIGINAL_SUMMARY)), None)
    other_docs = [i for i in all_docs if i is not orig]
    if orig is None:
        orig_err, orig_m = "missing", []
    else:
        orig_err = orig["error"]
        orig_m = [] if orig_err else mark_doc(orig)
    copies = []
    for i in other_docs:
        m = [] if i["error"] else mark_doc(i)
        copies.append((i["path"], m))
        ident["summaries"].append({"path": i["path"], "error": i["error"], "markers": m})
    ident["summaries"].insert(0, {"path": ORIGINAL_SUMMARY, "original": True, "error": orig_err, "markers": orig_m})
    if orig is None:
        if len(other_docs) == 1:
            only = other_docs[0]
            if (not only["error"] and looks_like_summary(only) and only["name_draft"]
                    and bool(only["draft_marking"]) and not _final_presented(only)):
                o1, m1 = True, (f"{ORIGINAL_SUMMARY} absent; single clearly DRAFT-marked summary "
                                f"{only['path']} carried forward with DRAFT marking intact")
            else:
                o1, m1 = False, f"{ORIGINAL_SUMMARY} absent and {only['path']} is not a valid DRAFT-marked summary"
        else:
            o1, m1 = False, ("no summary .docx found" if not other_docs else
                             f"{ORIGINAL_SUMMARY} absent and multiple summary candidates exist "
                             f"({', '.join(i['path'] for i in other_docs)})")
    elif not orig_err and _is_untouched_summary_shell(orig) and len(other_docs) == 1:
        only = other_docs[0]
        if (not only["error"] and looks_like_summary(only) and only["name_draft"]
                and bool(only["draft_marking"]) and not _final_presented(only)):
            o1, m1 = True, (f"{ORIGINAL_SUMMARY} is untouched template shell; single clearly DRAFT-marked summary "
                            f"{only['path']} carried forward with DRAFT marking intact")
        else:
            o1, m1 = _carry_decision("summary", ORIGINAL_SUMMARY, orig_m, orig_err, copies)
    else:
        o1, m1 = _carry_decision("summary", ORIGINAL_SUMMARY, orig_m, orig_err, copies)
        if orig is not None and not orig_err and not orig["draft_marking"]:
            o1 = False
            m1 += f"; {ORIGINAL_SUMMARY} has no DRAFT marking in title block/header"
    ok &= o1
    parts.append(m1)
    wcopies, worig_m, worig_err = [], [], None
    for rel in ctx.wb_candidates():
        w = ctx.wb(rel)
        m = [] if w.error else (w.facts["approval_markers"] if s3 else w.facts["batch_markers"])
        ident["workbooks"].append({"name": rel, "error": w.error, "markers": m})
        if _is_original(rel, ORIGINAL_WB):
            worig_m, worig_err = m, w.error
        else:
            wcopies.append((rel, m))
    if ORIGINAL_WB not in ctx.wb_candidates():
        ident["workbooks"].insert(0, {"name": ORIGINAL_WB, "error": "missing"})
        if len(wcopies) == 1 and not ctx.wb(wcopies[0][0]).error:
            o2, m2 = True, f"{ORIGINAL_WB} absent; single workbook {wcopies[0][0]} carried forward"
        else:
            o2, m2 = _carry_decision("workbook", ORIGINAL_WB, [], "missing", wcopies)
    else:
        o2, m2 = _carry_decision("workbook", ORIGINAL_WB, worig_m, worig_err, wcopies)
    ok &= o2
    parts.append(m2)
    ctx.obs["draft_identity"] = ident
    return result(cid, ok, " | ".join(parts))


def final_issue(ctx, cid, iso, need_no_placeholders):
    summ, sw, wrel, ww = ctx.stage_pair()
    sel = ctx.obs["delivered_selection"]
    parts = ["non-DRAFT summary candidates: " + (short([f"{c['path']} dates={c['issue_dates']}"
                                                         for c in sel["summary_candidates"] if not c["draft_marking"]], 6)
                                                  or "none"), sw]
    ok = summ is not None
    if summ:
        parts.append(f"selected {summ['path']}: date line {summ['date_line']!r}, issue dates {summ['issue_dates']}, "
                     f"DRAFT marking {summ['draft_marking'] or 'none'}, placeholders {summ['placeholders'] or 'none'}")
        if need_no_placeholders and summ["placeholders"]:
            ok = False
    w = ctx.wb(wrel)
    if w.error:
        ok = False
        parts.append(f"delivered workbook {wrel}: {w.error}")
    else:
        parts.append(f"delivered workbook {wrel} ({ww})")
    return result(cid, ok, " | ".join(parts))


# ================================================================ stage 1
def s1_band_max(ctx, cid):
    ok, why = ctx.wb_ok()
    if not ok:
        return result(cid, False, why)
    cov = coverage(ctx)
    bad, n = compare_emps(ctx, ctx.ref["order"], RESULT_FIELDS)
    cap = []
    for eid in ctx.ref["order"]:
        nb, e = calc_num(ctx.calc, eid, "new_base")
        if nb is not None and nb > ctx.ref["employees"][eid]["band_max"] + 0.005:
            cap.append(f"{eid} new base {fmt(nb)} > Band Max {fmt(ctx.ref['employees'][eid]['band_max'])}")
    capped = [e for e in ctx.ref["order"] if ctx.ref["employees"][e]["lump"] > 0]
    lt, lerr = calc_sum(ctx, "lump")
    lump_ok = near(lt, ctx.ref["lump_total"])
    po, perr = pools_ok(ctx)
    cv, caddr, cerr = pool_cell(ctx.pools, "Company Total", "spend") if po else (None, None, perr)
    exp_c = ctx.ref["dept"]["Company Total"]["spend"]
    tot_ok = num(cv) is not None and near(num(cv), exp_c)
    passed = not cov and not bad and not cap and lump_ok and tot_ok
    ev = (f"Calc '{ctx.calc['sheet']}' rows={len(ctx.calc['rows'])}; coverage: {short(cov) or 'all 38 IDs, unique'}; "
          f"{n} amounts compared vs Guidelines recomputation: {len(bad)} mismatches {short(bad)}; Band Max breaches: "
          f"{short(cap) or 'none'}; capped employees {','.join(capped)}; Calc lump-sum total {fmt(lt)} vs "
          f"{fmt(ctx.ref['lump_total'])}{' ' + short(lerr, 2) if lerr else ''}; reported company recurring merit "
          f"{'!'.join(caddr) if caddr else ''} {cerr or fmt(num(cv)) if num(cv) is not None or cerr else repr(cv)}"
          f" vs {fmt(exp_c)} (lump sums excluded)")
    return result(cid, passed, ev)


def s1_pools(ctx, cid):
    ok, why = ctx.wb_ok()
    if not ok:
        return result(cid, False, why)
    po, perr = pools_ok(ctx)
    msgs, allok = [], po
    if not po:
        msgs.append(perr)
    else:
        for d in model.DEPTS:
            o, m, _ = eval_dept(ctx, d)
            allok &= o
            msgs += m
    o, m = eval_promo(ctx)
    allok &= o
    msgs += m
    ev = ("Dept_Pools and promotion block bound by header/label text; underlying recalculated values vs reference "
          "within $0.05 (number format ignored); status read against sign of remaining. "
          + (short(msgs, 12) or "all five departments and the promotion pool match"))
    return result(cid, allok, ev)


# ================================================================ stage 2
def s2_batch(ctx, cid):
    ok, why = ctx.wb_ok()
    if not ok:
        return result(cid, False, why)
    cov = coverage(ctx, BATCH_IDS)
    msgs = []
    R = ctx.ref["employees"]
    fields = {"E1009": ["dept", "months", "rec_merit", "new_base", "lump", "rec_promo"],
              "E1012": ["eff_level", "rec_promo", "rec_merit", "new_base", "lump"],
              "E4002": ["months", "rec_merit", "lump", "new_base", "rec_promo"],
              "E3001": ["dept", "months", "rec_merit", "new_base", "lump", "rec_promo"]}
    for eid, fl in fields.items():
        if eid not in ctx.calc["rows"]:
            continue
        for f in fl:
            if f in ("dept", "eff_level"):
                exp = R[eid]["dept"] if f == "dept" else R[eid]["effective_level"]
                v, e = calc_value(ctx.calc, eid, f)
                if e:
                    msgs.append(f"{eid} {FIELD_LABEL[f]}: {e}")
                elif norm(v) != norm(exp):
                    msgs.append(f"{eid} {FIELD_LABEL[f]} {v!r} vs {exp!r}")
                continue
            exp = float(R[eid][REF_KEY[f]])
            v, e = calc_num(ctx.calc, eid, f)
            if e:
                msgs.append(f"{eid} {FIELD_LABEL[f]}: {e}")
            elif abs(v - exp) > TOL:
                msgs.append(f"{eid} {FIELD_LABEL[f]} {fmt(v)} vs {fmt(exp)}")
    a, _ = calc_num(ctx.calc, "E4002", "rec_merit")
    b, _ = calc_num(ctx.calc, "E4002", "lump")
    if a is not None and b is not None and abs(a + b - R["E4002"]["computed_merit"]) > TOL:
        msgs.append(f"E4002 computed merit {fmt(a + b)} vs {fmt(R['E4002']['computed_merit'])}")
    po, perr = pools_ok(ctx)
    bv, _, berr = pool_cell(ctx.pools, "Product", "budget") if po else (None, None, perr)
    if berr or not near(num(bv), ctx.ref["dept"]["Product"]["budget"]):
        msgs.append(f"Product budget {berr or fmt(num(bv))} vs {fmt(ctx.ref['dept']['Product']['budget'])} "
                    f"(E1009 funded in Product)")
    ev = (f"batch employees only (E1009 Product/12 mo/10,450/200,450; E1012 L4/10,680/7,342.50/151,522.50/0; "
          f"E4002 12 mo/4,800+808=5,608/145,000; E3001 Sales/12 mo/6,720/174,720) and Product budget 41,982.50: "
          f"coverage {short(cov) or 'ok'}; {short(msgs, 10) or 'all match'}")
    return result(cid, not cov and not msgs, ev)


def s2_pools(ctx, cid):
    ok, why = ctx.wb_ok()
    if not ok:
        return result(cid, False, why)
    po, perr = pools_ok(ctx)
    msgs, allok = [], po
    if not po:
        msgs.append(perr)
    else:
        for d in model.DEPTS:
            o, m, _ = eval_dept(ctx, d)
            allok &= o
            msgs += m
        o, m, _ = eval_dept(ctx, "Company Total", need_status=True, optional_status=True)
        allok &= o
        msgs += m
    o, m = eval_promo(ctx)
    allok &= o
    msgs += m
    lt, lerr = calc_sum(ctx, "lump")
    cnt = sum(1 for eid in ctx.ref["order"] if (calc_num(ctx.calc, eid, "lump")[0] or 0) > 0.005)
    exp_cnt = len(ctx.ref["lumps"])
    if lerr or not near(lt, ctx.ref["lump_total"]) or cnt != exp_cnt:
        allok = False
        msgs.append(f"lump sums: {cnt} totalling {fmt(lt)} vs {exp_cnt} totalling {fmt(ctx.ref['lump_total'])}"
                    + (f" ({short(lerr, 2)})" if lerr else ""))
    ev = "recomputed amended-roster pools vs Dept_Pools (by header) and promotion block, $0.05 on underlying values: " + (
        short(msgs, 14) or "all departments, company total, promotion pool and 7 lump sums match")
    return result(cid, allok, ev)


# ================================================================ stage 4
def s4_prelock(ctx, cid):
    ok, why = ctx.wb_ok()
    if not ok:
        return result(cid, False, why)
    msgs = list(coverage(ctx, ["E2007"]))
    if not msgs:
        rv, e = calc_num(ctx.calc, "E2007", "rating")
        if e or rv != 4:
            msgs.append(f"E2007 rating {e or rv} vs 4")
        lv, e = calc_value(ctx.calc, "E2007", "eff_level")
        if e or norm(lv) != "l3":
            msgs.append(f"E2007 effective level {e or repr(lv)} vs L3")
        for f, exp in (("base", 112000.0), ("rec_promo", 0.0), ("rec_merit", 4480.0)):
            v, e = calc_num(ctx.calc, "E2007", f)
            if e or not near(v, exp):
                msgs.append(f"E2007 {FIELD_LABEL[f]} {e or fmt(v)} vs {fmt(exp)}")
        pv, e = calc_num(ctx.calc, "E2007", "promo_inc")
        if e is None and not near(pv, 0.0):
            msgs.append(f"E2007 promotion increase {fmt(pv)} vs 0 (promotion status unchanged)")
    po, perr = pools_ok(ctx)
    if po:
        for d in ("Product", "Company Total"):
            _, m, _ = eval_dept(ctx, d, need_status=False)
            msgs += m
    else:
        msgs.append(perr)
    ev = (f"delivered workbook {ctx.wb_path.name}: E2007 rating 4 / L3 / 112,000 / no promotion / merit 4,480; "
          f"Product 41,982.50 / 45,426.25 / -3,443.75; company 166,102.29 / 166,417.08 / -314.79 (or overage "
          f"column); $0.05: {short(msgs, 10) or 'all match'}")
    return result(cid, not msgs, ev)


# ================================================================ stage 5
def s5_target(ctx, cid):
    ok, why = ctx.wb_ok()
    if not ok:
        return result(cid, False, why)
    pa = product_analysis(ctx)
    msgs = list(pa["errors"])
    if set(pa["members"]) != set(pa["ref_members"]):
        msgs.append(f"Calc Product rows {sorted(pa['members'])} vs lock-date Product {sorted(pa['ref_members'])}")
    if pa["uniform_bad"]:
        msgs.append("reduction not uniform: " + short(pa["uniform_bad"], 4))
    if pa["calc_spend"] is None or abs(pa["calc_spend"] - pa["target"]) > pa["tol"]:
        msgs.append(f"Product recurring merit {fmt(pa['calc_spend'])} vs {fmt(pa['target'])} "
                    f"(residual {fmt(pa['residual'])}, tolerance {pa['tol']:.2f})")
    rep_txt = []
    for key, calc_tot in (("Product", pa["calc_product_rows_sum"]), ("Company Total", pa["calc_company_sum"])):
        r = pa["reported_spend"][key]
        if r["error"]:
            msgs.append(f"Dept_Pools {key} spend: {r['error']}")
        elif num(r["value"]) is None or calc_tot is None or abs(num(r["value"]) - calc_tot) > TOL:
            msgs.append(f"Dept_Pools {key} spend {r['cell']}={r['value']!r} vs the workbook's own Calc sum "
                        f"{fmt(calc_tot)}")
        rep_txt.append(f"{key} {r['cell']}={fmt(num(r['value'])) if num(r['value']) is not None else repr(r['value'])}"
                       f" vs Calc sum {fmt(calc_tot)}")
    ev = (f"workbook {ctx.wb_path.name}: sum of its recurring-merit column over the 8 lock-date Product rows = "
          f"{fmt(pa['calc_spend'])} vs target 41,982.50 + 2,500 = {fmt(pa['target'])} (unrounded residual "
          f"{pa['residual']!r}); workbook factor {pa['f_wb']!r} ({pa['f_source']}), exact {pa['exact_factor']:.10f}; "
          f"tolerance {pa['tol_reason']}; reported pool figures: {'; '.join(rep_txt)} ($0.05); "
          + (short(msgs, 8) or "all conditions met"))
    return result(cid, not msgs, ev)


def s5_bandmax(ctx, cid):
    ok, why = ctx.wb_ok()
    if not ok:
        return result(cid, False, why)
    pa = product_analysis(ctx)
    msgs = list(coverage(ctx, ["E2006"]))
    p = pa["per_employee"].get("E2006")
    if not msgs:
        v = {f: calc_num(ctx.calc, "E2006", f) for f in ("rec_merit", "lump", "rec_promo")}
        msgs += [f"E2006 {FIELD_LABEL[f]}: {e}" for f, (_, e) in v.items() if e]
        if not msgs:
            rm, lp, rp = v["rec_merit"][0], v["lump"][0], v["rec_promo"][0]
            if not near(rm, 9000.0):
                msgs.append(f"E2006 recurring merit {fmt(rm)} vs 9,000.00 (band room)")
            if not near(rp, 0.0):
                msgs.append(f"E2006 recurring promotion {fmt(rp)} vs 0")
            if pa["f_wb"] is None or p is None:
                msgs.append("workbook factor / E2006 matrix merit not derivable")
            elif abs(pa["f_wb"] - 1) <= 1e-9:
                msgs.append("no Product reduction factor applied (effective factor 1), so the Band Max interaction "
                            "under the CFO decision is not shown")
            else:
                exp_l = pa["f_wb"] * p["matrix_merit"] - 9000.0
                if not near(lp, exp_l):
                    msgs.append(f"E2006 lump {fmt(lp)} vs {fmt(exp_l)} = {pa['f_wb']:.10f} x {fmt(p['matrix_merit'])}"
                                f" - 9,000")
    ev = ("E2006 base 169,000, L4 max 178,000 -> 9,000 room; recurring merit must be 9,000, lump = computed merit at "
          f"the workbook's own factor ({pa['f_wb']!r}) - 9,000 within $0.05, promotion 0: " + (short(msgs, 6) or "all hold"))
    return result(cid, not msgs, ev)


def s5_unchanged(ctx, cid):
    ok, why = ctx.wb_ok()
    if not ok:
        return result(cid, False, why)
    msgs = list(coverage(ctx))
    po, perr = pools_ok(ctx)
    for d in ("Engineering", "Sales", "Customer Success", "G&A"):
        sv, _, err = pool_cell(ctx.pools, d, "spend") if po else (None, None, perr)
        if err or not near(num(sv), ctx.ref["dept"][d]["spend"]):
            msgs.append(f"{d} spend {err or repr(sv)} vs {fmt(ctx.ref['dept'][d]['spend'])}")
    it = ctx.promo.get("items", {}).get("spend")
    if ctx.promo.get("error") or it is None or not near(num(it["value"]), ctx.ref["promo"]["spend"]):
        msgs.append(f"promotion spend {ctx.promo.get('error') or (it or {}).get('value')!r} vs "
                    f"{fmt(ctx.ref['promo']['spend'])}")
    ids = [e for e in ctx.ref["order"] if e in ctx.calc["rows"]]
    pb, n = compare_emps(ctx, ids, ["rec_promo"])
    msgs += pb
    lb, _ = compare_emps(ctx, [e for e in ("E3006", "E1006", "E5005") if e in ctx.calc["rows"]], ["lump"])
    msgs += lb
    ev = ("Eng 51,860.83 / Sales 29,719.17 / CS 19,042.08 / G&A 20,368.75 spend, promotion spend 36,120.00, "
          f"{n} recurring promotion amounts, VP lumps E3006 5,560 / E1006 3,320 / E5005 3,888: "
          + (short(msgs, 8) or "all unchanged"))
    return result(cid, not msgs, ev)


# ================================================================ observations
def collect_observations(ctx):
    obs = ctx.obs
    R = ctx.ref
    obs["reference"] = {
        "basis": "recomputed from trusted Roster + Guidelines + public roster changes in force at this stage",
        "departments": {k: {f: r4(v[f]) for f in ("headcount", "budget", "spend", "remaining")} for k, v in R["dept"].items()},
        "promotion_pool": {f: r4(R["promo"][f]) for f in ("budget", "spend", "remaining", "count")},
        "lump_sums": [{"id": l["id"], "name": l["name"], "amount": r4(l["lump"]), "above_3000_vp": l["needs_vp"]}
                      for l in R["lumps"]],
        "lump_total": r4(R["lump_total"]),
        "employees": {e: {k: (r4(v) if isinstance(v, float) else v) for k, v in R["employees"][e].items()
                          if k in ("dept", "effective_level", "base", "rating", "eligible_months", "computed_merit",
                                   "rec_promo", "rec_merit", "lump", "new_base")} for e in R["order"]},
        "below_band_min_after_merit": [
            {"id": e, "level": R["employees"][e]["level"], "base": R["employees"][e]["base"],
             "new_base": r4(R["employees"][e]["new_base"]),
             "band_min": model.BAND_MIN[R["employees"][e]["effective_level"]]}
            for e in R["order"] if R["employees"][e]["base"] < model.BAND_MIN[R["employees"][e]["level"]]],
    }
    if R["product_factor"] is not None:
        obs["reference"]["product_factor"] = R["product_factor"]
        obs["reference"]["product_target"] = r4(R["product_target"])
    ctx.summaries()
    if ctx.stage in STAGE_ISO:
        ctx.stage_pair()
    ctx.load_workbook()
    obs["workbook_candidate_facts"] = [w.facts if not w.error else {"name": r, "error": w.error}
                                       for r, w in sorted(ctx._wbs.items())]
    if ctx.wb_error:
        obs["workbook_actual"] = {"error": ctx.wb_error}
        return obs
    act = {"source": f"{ctx.wb_path.name} after LibreOffice recalculation"}
    if not ctx.calc.get("error"):
        emps = {}
        for eid, row in sorted(ctx.calc["rows"].items(), key=lambda x: x[1]):
            d = {"row": row}
            for f in ("dept", "eff_level", "rating", "base", "months", "promo_inc", "merit_inc",
                      "rec_promo", "rec_merit", "lump", "new_base"):
                v, e = calc_value(ctx.calc, eid, f)
                d[f] = r4(v) if isinstance(v, float) else (v if e is None else f"<{e}>")
            emps[eid] = d
        act["employees"] = emps
        lumps = [{"id": e, "amount": r4(num(v["lump"]))} for e, v in emps.items()
                 if num(v["lump"]) is not None and num(v["lump"]) > 0.005]
        act["lump_sums"] = lumps
        act["lump_total"] = r4(sum(x["amount"] for x in lumps))
    else:
        act["calc_error"] = ctx.calc["error"]
    if not ctx.pools.get("error"):
        blk = {}
        for key in model.DEPTS + ["Company Total"]:
            row = {}
            for f in ("budget", "spend", "remaining", "overage", "status"):
                v, addr, err = pool_cell(ctx.pools, key, f)
                row[f] = {"value": r4(v) if isinstance(v, float) else v, "cell": "!".join(addr)} if not err else f"<{err}>"
            blk[key] = row
        act["dept_pools"] = blk
        ws = ctx.pools["ws"]
        act["dept_pools_sheet_cells"] = {c.coordinate: (r4(c.value) if isinstance(c.value, float) else
                                                        (c.value.isoformat() if hasattr(c.value, "isoformat") else c.value))
                                         for row in ws.iter_rows() for c in row if c.value not in (None, "")}
    else:
        act["dept_pools_error"] = ctx.pools["error"]
    act["promotion_pool"] = ({k: {"label": v["label"], "value": r4(v["value"]) if isinstance(v["value"], float) else v["value"],
                                  "cell": "!".join(v["addr"])} for k, v in ctx.promo["items"].items()}
                             if not ctx.promo.get("error") else {"error": ctx.promo["error"]})
    extra = {}
    for w in ctx.vwb.worksheets:
        t = w.title.casefold()
        if (("change" in t and "log" in t) or "approv" in t or "exception" in t or "action" in t
                or "factor" in t or "adjust" in t or "register" in t or "decision" in t or "status" in t):
            rows = []
            for i, row in enumerate(w.iter_rows(min_row=1, max_row=min(w.max_row, 120), values_only=True), 1):
                if any(v not in (None, "") for v in row):
                    rows.append([i] + [r4(v) if isinstance(v, float) else (v.isoformat() if hasattr(v, "isoformat") else v)
                                       for v in row])
            extra[w.title] = rows
    act["other_sheets"] = extra
    act["change_log"] = next((v for k, v in extra.items() if "change" in k.casefold() and "log" in k.casefold()),
                             "<no Change_Log sheet>")
    act["sheet_names"] = [w.title for w in ctx.vwb.worksheets]
    if ctx.stage == "s5_cfo_product_decision" and not ctx.calc.get("error"):
        pa = product_analysis(ctx)
        act["product_exception"] = {
            "workbook_factor": pa["f_wb"], "factor_source": pa["f_source"], "factor_cells": pa["factor_hits"],
            "calc_members": pa["members"], "lock_date_members": pa["ref_members"],
            "product_recurring_merit_sum": r4(pa["calc_spend"]), "residual_vs_44482_50_unrounded": pa["residual"],
            "residual_after_per_employee_cent_rounding": pa["residual_after_per_employee_cent_rounding"],
            "reported_spend": pa["reported_spend"], "calc_product_rows_sum": pa["calc_product_rows_sum"],
            "calc_company_sum": pa["calc_company_sum"],
            "recomputed_at_workbook_factor": pa.get("recomputed_at_workbook_factor"),
            "per_employee": {e: {k: (v if k in ("factor", "source") else r4(v)) for k, v in p.items()}
                             for e, p in pa["per_employee"].items()},
            "non_uniform": pa["uniform_bad"], "tolerance_applied": pa["tol"], "tolerance_reason": pa["tol_reason"],
            "text_cells_mentioning_rounding": pa["rounding_text_cells"], "errors": pa["errors"][:10]}
    obs["workbook_actual"] = act
    return obs


# ================================================================ registry
STAGE_CHECKS = {
    "s1_complete_baseline": [
        ("s1_chk_band_max_split", s1_band_max),
        ("s1_chk_pools", s1_pools),
        ("s1_chk_formula_driven", s1_live),
        ("s1_chk_draft_hold", check_draft_hold),
    ],
    "s2_hr_ops_batch": [
        ("s2_chk_batch_applied", s2_batch),
        ("s2_chk_pools_after_batch", s2_pools),
        ("s2_chk_live_formulas", s2_live),
        ("s2_chk_draft_hold", check_draft_hold),
    ],
    "s3_approvals": [
        ("s3_chk_draft_hold", check_draft_hold),
    ],
    "s4_final_package": [
        ("s4_chk_prelock_applied", s4_prelock),
        ("s4_chk_final_issue", lambda c, i: final_issue(c, i, "2026-12-02", True)),
        ("s4_chk_live_formulas", s4_live),
    ],
    "s5_cfo_product_decision": [
        ("s5_chk_product_target", s5_target),
        ("s5_chk_bandmax_interaction", s5_bandmax),
        ("s5_chk_unchanged", s5_unchanged),
        ("s5_chk_reissue", lambda c, i: final_issue(c, i, "2026-12-08", False)),
        ("s5_chk_live_formulas", s5_live),
    ],
}
