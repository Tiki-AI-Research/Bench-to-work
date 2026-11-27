#!/usr/bin/env python3
"""Programmatic checks for the HarborPoint Oct 2026 SLA credit task.

Usage: verifier.py --workspace PATH --stage STAGE_ID
Prints one JSON object {"checks": [...], "observations": {...}} for the stage.
Trusted originals are read from beside this file under their task-relative
paths; the Worker submission is read (never modified) from --workspace.
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify import workbook as W  # noqa: E402
from verify.reference import CODE_DIR, TRUSTED_WORKBOOK_REL, compute_reference  # noqa: E402
from verify.texts import collect_texts, inventory  # noqa: E402

STAGES = ("s1_inherited_claim", "s2_sre_overlap", "s3_customer_dispute", "s4_decision_closeout")
UPTIME_TOL_FRAC = 0.00005
UPTIME_TOL_PCT = 0.005
EPS = 1e-9
MAX_HITS = 40


# ---------------- predicates ----------------
def uptime_pred(frac4):
    pct = round(frac4 * 100, 2)
    return lambda v: abs(v - frac4) <= UPTIME_TOL_FRAC + EPS or abs(v - pct) <= UPTIME_TOL_PCT + EPS


def tier_pred(frac):
    pct = round(frac * 100, 6)
    return lambda v: abs(v - frac) <= EPS or abs(v - pct) <= EPS


def equal_pred(x):
    return lambda v: abs(v - x) <= 1e-6


def fmt_hits(hits):
    if not hits:
        return "none"
    shown = ", ".join("%s=%r" % (h["cell"], h["value"]) for h in hits[:6])
    return shown + (" (+%d more)" % (len(hits) - 6) if len(hits) > 6 else "")


# ---------------- workbook evidence ----------------
def load_workbook_evidence(workspace):
    """Return dict with status in {ok, missing, corrupt, not_recalculable} and cells."""
    ev = {"status": None, "cells": None, "order": None, "names": None}
    path, info = W.locate(workspace)
    ev["locate"] = info
    if path is None:
        ev["status"] = "missing"
        if info.get("declared_path_status", "").startswith("not readable"):
            ev["detail"] = "declared workbook path %s %s" % (W.PRIMARY_REL, info["declared_path_status"])
        else:
            ev["detail"] = "declared path %s missing; fallback search under %s/: %s (candidates %s)" % (
                W.PRIMARY_REL, W.SEARCH_REL, info.get("selection"), info.get("fallback_candidates"))
        return ev
    ev["path"] = info["used_path"]
    try:
        import openpyxl
        openpyxl.load_workbook(path, data_only=False).close()
    except W.ACCESS_ERRORS as exc:
        ev["status"] = "missing"
        ev["detail"] = "cannot open %s: %s" % (info["used_path"], type(exc).__name__)
        return ev
    except W.CORRUPT_ERRORS as exc:
        ev["status"] = "corrupt"
        ev["detail"] = "openpyxl cannot parse %s: %s: %s" % (info["used_path"], type(exc).__name__, exc)
        return ev
    recalc_path, tmp, err = W.recalc_copy(path, CODE_DIR / TRUSTED_WORKBOOK_REL)
    ev["tmpdir"] = tmp
    if recalc_path is None:
        ev["status"] = "not_recalculable"
        ev["detail"] = ("LibreOffice could not recalculate a copy of %s (trusted control converted fine): %s"
                        % (info["used_path"], err))
        return ev
    cells, order, names = W.load_cells(path, recalc_path)
    ev.update(status="ok", cells=cells, order=order, names=names,
              detail="recalculated a temp copy of %s with LibreOffice headless (forced recalc on load); "
                     "formulas from the submitted file, values from the recalculated copy" % info["used_path"])
    return ev


def scenario_hits(cells, sc, include_minutes):
    h = {}
    if include_minutes:
        h["downtime_%d_any_cell" % sc["downtime_min"]] = W.find_hits(cells, equal_pred(sc["downtime_min"]), False)
    h["uptime_%.2f_formula" % sc["uptime_pct_2dp"]] = W.find_hits(cells, uptime_pred(sc["uptime_fraction_4dp"]), True)
    h["tier_%g_formula" % sc["tier_fraction"]] = W.find_hits(cells, tier_pred(sc["tier_fraction"]), True)
    h["credit_%g_formula" % sc["credit"]] = W.find_hits(cells, equal_pred(sc["credit"]), True)
    return h


def unavailable_check(cid, ev):
    return {"id": cid, "passed": False,
            "evidence": "Workbook evidence unavailable (%s): %s" % (ev["status"], ev.get("detail"))}


_COORD_RE = re.compile(r"^([A-Za-z]{1,3})(\d+)$")
_RECON_LABEL_RE = re.compile(
    r"\b(as[- ]received|prior|old|legacy|previous|dana|30[- ]day|43,?200|incorrect|"
    r"error|wrong|superseded|recon\w*|variance|diff\w*|original|unadjusted|before\s+correction)\b",
    re.I,
)


def _dependents_map(cells, names, seed_cell_ids):
    """Return ({cell_id: {'root': seed_id, 'via': str, 'formula': str}}, tokenize_errors)
    for all formula cells transitively referencing any cell in seed_cell_ids."""
    queue = [(cid, cid) for cid in seed_cell_ids]
    visited = {}
    seen_seeds = set(seed_cell_ids)
    tok_errs = []
    seen_errs = set()
    while queue:
        cur, root = queue.pop(0)
        sheet, coord = cur.rsplit("!", 1)
        found, errs = W.references_to(cells, names, sheet, coord)
        for e in errs:
            if e not in seen_errs:
                seen_errs.add(e)
                tok_errs.append(e)
        for f in found:
            fcid = f["cell"]
            if fcid not in visited:
                visited[fcid] = {"root": root, "via": f["via"], "formula": f["formula"]}
                if fcid not in seen_seeds:
                    seen_seeds.add(fcid)
                    queue.append((fcid, root))
    return visited, tok_errs


def _inline_44640_cells(cells, total_minutes):
    pat = re.compile(r"(?<!\d)(?:%d|31\s*\*\s*1440|1440\s*\*\s*31)(?!\d)" % total_minutes)
    out = []
    for sheet, sc in cells.items():
        for coord, cv in sc.items():
            f = cv["formula"]
            if f and pat.search(f):
                out.append("%s!%s" % (sheet, coord))
    return out


def _driven_44640_chain(cells, names, ref, scope_sheets=None):
    """Find formula cells for (99.47% uptime, 10% tier, $4,800 credit) driven by the 44,640 base."""
    good = ref["all_counted"]
    c44640 = W.find_hits(cells, equal_pred(ref["total_minutes"]), False)
    inline_44640 = _inline_44640_cells(cells, ref["total_minutes"])
    base_seeds = list(dict.fromkeys([h["cell"] for h in c44640] + inline_44640))
    dep_base, tok_err = _dependents_map(cells, names, base_seeds)
    driven_ids = set(dep_base) | set(inline_44640)

    def in_scope(hit):
        return scope_sheets is None or hit["cell"].rsplit("!", 1)[0].casefold() in scope_sheets

    up_all = [h for h in W.find_hits(cells, uptime_pred(good["uptime_fraction_4dp"]), True) if in_scope(h)]
    up_driven = [h for h in up_all if h["cell"] in driven_ids]

    dep_up, err2 = _dependents_map(cells, names, [h["cell"] for h in up_driven])
    for e in err2:
        if e not in tok_err:
            tok_err.append(e)
    tier_reach = set(dep_up) | (driven_ids if up_driven else set())
    tier_all = [h for h in W.find_hits(cells, tier_pred(good["tier_fraction"]), True) if in_scope(h)]
    tier_driven = [h for h in tier_all if h["cell"] in tier_reach]

    dep_tier, err3 = _dependents_map(cells, names, [h["cell"] for h in tier_driven])
    for e in err3:
        if e not in tok_err:
            tok_err.append(e)
    cr_reach = set(dep_tier) | (tier_reach if tier_driven else set())
    cr_all = [h for h in W.find_hits(cells, equal_pred(good["credit"]), True) if in_scope(h)]
    cr_driven = [h for h in cr_all if h["cell"] in cr_reach]

    return {
        "c44640": c44640,
        "inline_44640": inline_44640,
        "uptime_all": up_all,
        "uptime": up_driven,
        "tier_all": tier_all,
        "tier": tier_driven,
        "credit_all": cr_all,
        "credit": cr_driven,
        "tokenize_errors": tok_err,
        "ok": bool(up_driven) and bool(tier_driven) and bool(cr_driven),
    }


def _classify_stale_uptime_09945(cells, names, ref, up_driven):
    """Split formula cells ~0.9945/99.45 into (active_bad, recon_allowed)."""
    old = ref["as_received"]
    bad_all = W.find_hits(cells, uptime_pred(old["uptime_fraction_4dp"]), True)
    if not bad_all:
        return [], []
    tier_cells = {h["cell"] for h in W.find_hits(cells, tier_pred(ref["all_counted"]["tier_fraction"]), True)}
    dep_up, _ = _dependents_map(cells, names, [h["cell"] for h in up_driven])
    active_bad, recon_ok = [], []
    for bh in bad_all:
        sheet, coord = bh["cell"].rsplit("!", 1)
        if sheet.casefold() == "uptime_calc" and coord.upper() == "B6":
            active_bad.append(bh)
            continue
        m = _COORD_RE.match(coord)
        col_str, row_num = (m.group(1).upper(), int(m.group(2))) if m else ("", -1)
        ctx_strings = []
        for c_coord, cv in cells.get(sheet, {}).items():
            if not isinstance(cv.get("value"), str):
                continue
            cm = _COORD_RE.match(c_coord)
            if not cm:
                continue
            cc, cr = cm.group(1).upper(), int(cm.group(2))
            if cr == row_num or (cc == col_str and cr < row_num):
                ctx_strings.append(cv["value"])
        has_recon_label = any(_RECON_LABEL_RE.search(s) for s in ctx_strings)
        dep_bh, _ = _dependents_map(cells, names, [bh["cell"]])
        feeds_tier = bool(set(dep_bh) & tier_cells)
        in_variance_diff = bool(set(dep_bh) & set(dep_up)) and not feeds_tier
        if has_recon_label or in_variance_diff:
            recon_ok.append(bh)
        else:
            active_bad.append(bh)
    return active_bad, recon_ok


def check_s1(ev, ref, hits_out):
    cid = "s1_workbook_basis_corrected"
    if ev["status"] != "ok":
        return unavailable_check(cid, ev)
    cells, names = ev["cells"], ev["names"]
    good, old = ref["all_counted"], ref["as_received"]
    chain = _driven_44640_chain(cells, names, ref)
    active_bad, recon_09945 = _classify_stale_uptime_09945(cells, names, ref, chain["uptime"])
    c43200 = W.find_hits(cells, equal_pred(ref["as_received_total_minutes"]), False)
    refs_43200 = []
    tok_err = list(chain["tokenize_errors"])
    for hit in c43200:
        sheet, coord = hit["cell"].rsplit("!", 1)
        found, errs = W.references_to(cells, names, sheet, coord)
        for e in errs:
            if e not in tok_err:
                tok_err.append(e)
        for f in found:
            refs_43200.append({"target": hit["cell"], **f})

    h = {
        "uptime_%.2f_formula" % good["uptime_pct_2dp"]: chain["uptime"],
        "tier_%g_formula" % good["tier_fraction"]: chain["tier"],
        "credit_%g_formula" % good["credit"]: chain["credit"],
        "active_forbidden_uptime_%.2f_formula" % old["uptime_pct_2dp"]: active_bad,
        "recon_uptime_%.2f_formula_info" % old["uptime_pct_2dp"]: recon_09945,
        "cells_equal_%d_info" % ref["as_received_total_minutes"]: c43200,
        "formulas_referencing_%d_cells_info" % ref["as_received_total_minutes"]: refs_43200,
        "cells_equal_%d_info" % ref["total_minutes"]: chain["c44640"],
    }
    if tok_err:
        h["formula_tokenize_errors"] = tok_err
    hits_out.update(h)
    a_ok = chain["ok"]
    b_ok = not active_bad
    ev_txt = (
        "%s. (a) 44,640-driven formula chain — cells=44,640: %s; uptime ~%.4f/%.2f: %s; "
        "tier %g/%g: %s; credit %g: %s -> %s. "
        "(b) active stale ~%.4f/%.2f (43,200 basis) formula cells: %s (reconciliation/audit 43,200 refs: %s) -> %s. "
        "Expected from trusted workbook + Exhibit C 1.4: (%d-%d)/%d = %.5f." % (
            ev["detail"], fmt_hits(chain["c44640"]),
            good["uptime_fraction_4dp"], good["uptime_pct_2dp"], fmt_hits(chain["uptime"]),
            good["tier_fraction"], good["tier_fraction"] * 100, fmt_hits(chain["tier"]),
            good["credit"], fmt_hits(chain["credit"]), "ok" if a_ok else "FAIL",
            old["uptime_fraction_4dp"], old["uptime_pct_2dp"], fmt_hits(active_bad),
            ("; ".join("%s uses %s via %s" % (r["cell"], r["target"], r["via"]) for r in refs_43200[:6]) or "none"),
            "ok" if b_ok else "FAIL",
            ref["total_minutes"], good["downtime_min"], ref["total_minutes"], good["uptime_exact"],
        )
    )
    return {"id": cid, "passed": a_ok and b_ok, "evidence": ev_txt}


def _set_result(h, labels):
    parts, ok = [], True
    for k in labels:
        parts.append("%s: %s" % (k, fmt_hits(h[k])))
        ok = ok and bool(h[k])
    return ok, "; ".join(parts)


def check_s2(ev, ref, hits_out):
    cid = "s2_workbook_exclusion_scenario"
    if ev["status"] != "ok":
        return unavailable_check(cid, ev)
    sc = ref["excluded"]
    h = scenario_hits(ev["cells"], sc, include_minutes=True)
    hits_out.update(h)
    ok, txt = _set_result(h, list(h))
    return {"id": cid, "passed": ok, "evidence": (
        "%s. Exclusion scenario (%d - %d in-window = %d min; (%d-%d)/%d = %.5f; tier %g; credit %g): %s -> %s." % (
            ev["detail"], ref["all_counted"]["downtime_min"], ref["sre_in_window_minutes"], sc["downtime_min"],
            ref["total_minutes"], sc["downtime_min"], ref["total_minutes"], sc["uptime_exact"],
            sc["tier_fraction"], sc["credit"], txt, "pass" if ok else "FAIL (missing value above)"))}


def check_s3(ev, ref, hits_out):
    cid = "s3_workbook_both_calcs"
    if ev["status"] != "ok":
        return unavailable_check(cid, ev)
    h1 = scenario_hits(ev["cells"], ref["all_counted"], include_minutes=True)
    h2 = scenario_hits(ev["cells"], ref["excluded"], include_minutes=True)
    hits_out.update({"without_exclusion": h1, "with_exclusion": h2})
    ok1, t1 = _set_result(h1, list(h1))
    ok2, t2 = _set_result(h2, list(h2))
    return {"id": cid, "passed": ok1 and ok2, "evidence": (
        "%s. Without exclusion {236; 99.47%%; 10%%; 4800}: %s -> %s. With 60-min exclusion {176; 99.61%%; 5%%; 2400}: %s -> %s." % (
            ev["detail"], t1, "ok" if ok1 else "FAIL", t2, "ok" if ok2 else "FAIL"))}


def check_s4(ev, ref, hits_out):
    cid = "s4_workbook_final_result"
    if ev["status"] != "ok":
        return unavailable_check(cid, ev)
    cells, names = ev["cells"], ev["names"]
    good, excl, old = ref["all_counted"], ref["excluded"], ref["as_received"]
    c236 = W.find_hits(cells, equal_pred(good["downtime_min"]), False)
    wb_chain = _driven_44640_chain(cells, names, ref, scope_sheets=None)
    active_bad, recon_09945 = _classify_stale_uptime_09945(cells, names, ref, wb_chain["uptime"])

    uc_scope = {"uptime_calc"}
    uc_sla_formulas = [
        h for h in (
            W.find_hits(cells, uptime_pred(good["uptime_fraction_4dp"]), True)
            + W.find_hits(cells, uptime_pred(excl["uptime_fraction_4dp"]), True)
            + W.find_hits(cells, uptime_pred(old["uptime_fraction_4dp"]), True)
            + W.find_hits(cells, equal_pred(good["credit"]), True)
            + W.find_hits(cells, equal_pred(excl["credit"]), True)
        )
        if h["cell"].rsplit("!", 1)[0].casefold() in uc_scope
    ]
    top_block_excluded = False
    uc_names = [s for s in cells if s.casefold() in uc_scope]
    if uc_names:
        uc_sheet = cells[uc_names[0]]
        a6_val = uc_sheet.get("A6", {}).get("value")
        b6_val = uc_sheet.get("B6", {}).get("value")
        c6_val = uc_sheet.get("C6", {}).get("value")
        if (
            isinstance(a6_val, str)
            and a6_val.strip().casefold().startswith("monthly uptime")
            and not W.is_number(c6_val)
            and W.is_number(b6_val)
            and uptime_pred(excl["uptime_fraction_4dp"])(float(b6_val))
        ):
            top_block_excluded = True

    if uc_sla_formulas:
        uc_chain = _driven_44640_chain(cells, names, ref, scope_sheets=uc_scope)
        uc_ok = uc_chain["ok"] and not top_block_excluded
        uc_note = "Uptime_Calc primary formula chain: %s" % (
            "ok" if uc_ok else ("FAIL (top summary block left on 99.61%/$2,400)" if top_block_excluded else "FAIL (missing 99.47%/10%/$4,800 formula chain on Uptime_Calc)")
        )
    else:
        uc_chain = None
        uc_ok = True
        uc_note = "Uptime_Calc has no separate SLA formulas (checked across workbook)"

    excl_info = scenario_hits(cells, excl, include_minutes=True)
    h = {
        "cells_equal_%d" % ref["total_minutes"]: wb_chain["c44640"],
        "downtime_%d_any_cell" % good["downtime_min"]: c236,
        "uptime_%.2f_formula" % good["uptime_pct_2dp"]: wb_chain["uptime"],
        "tier_%g_formula" % good["tier_fraction"]: wb_chain["tier"],
        "credit_%g_formula" % good["credit"]: wb_chain["credit"],
        "active_forbidden_uptime_%.2f_formula" % old["uptime_pct_2dp"]: active_bad,
        "recon_uptime_%.2f_formula_info" % old["uptime_pct_2dp"]: recon_09945,
        "retained_exclusion_comparison_info": excl_info,
    }
    if uc_chain is not None:
        h["uptime_calc_primary_chain"] = {
            "uptime": uc_chain["uptime"],
            "tier": uc_chain["tier"],
            "credit": uc_chain["credit"],
            "top_block_excluded": top_block_excluded,
        }
    if wb_chain["tokenize_errors"]:
        h["formula_tokenize_errors"] = wb_chain["tokenize_errors"]
    hits_out.update(h)

    base_ok = bool(wb_chain["c44640"] or wb_chain["inline_44640"]) and bool(c236)
    chain_ok = wb_chain["ok"] and uc_ok
    no_stale_ok = not active_bad
    passed = base_ok and chain_ok and no_stale_ok
    ev_txt = (
        "%s. Approved final SLA result — 44,640 base: %s; 236 downtime min: %s; "
        "44,640-driven formula chain {uptime ~%.4f/%.2f: %s; tier %g/%g: %s; credit %g: %s} -> %s; "
        "%s; active stale ~%.4f/%.2f formula cells: %s -> %s." % (
            ev["detail"],
            fmt_hits(wb_chain["c44640"]),
            fmt_hits(c236),
            good["uptime_fraction_4dp"], good["uptime_pct_2dp"], fmt_hits(wb_chain["uptime"]),
            good["tier_fraction"], good["tier_fraction"] * 100, fmt_hits(wb_chain["tier"]),
            good["credit"], fmt_hits(wb_chain["credit"]),
            "ok" if wb_chain["ok"] else "FAIL",
            uc_note,
            old["uptime_fraction_4dp"], old["uptime_pct_2dp"], fmt_hits(active_bad),
            "ok" if no_stale_ok else "FAIL",
        )
    )
    return {"id": cid, "passed": passed, "evidence": ev_txt}


CHECKS = {
    "s1_inherited_claim": [check_s1],
    "s2_sre_overlap": [check_s2],
    "s3_customer_dispute": [check_s3],
    "s4_decision_closeout": [check_s4],
}


def _trim_hits(obj):
    if isinstance(obj, list):
        return obj[:MAX_HITS] + ([{"note": "%d more not shown" % (len(obj) - MAX_HITS)}] if len(obj) > MAX_HITS else [])
    if isinstance(obj, dict):
        return {k: _trim_hits(v) for k, v in obj.items()}
    return obj


def run(workspace, stage):
    ws = Path(workspace)
    if not ws.is_dir():
        raise SystemExit("workspace is not a directory: %s" % workspace)
    ref = compute_reference()
    ev = load_workbook_evidence(ws)
    try:
        hits = {}
        checks = [fn(ev, ref, hits) for fn in CHECKS[stage]]
        inv, files = inventory(ws)
        docx_texts, other_texts, pdf_texts, text_errors = collect_texts(ws, inv, files)
        wb_obs = {"status": ev["status"], "path": ev.get("path"), "locate": ev.get("locate"),
                  "detail": ev.get("detail")}
        if ev["status"] == "ok":
            wb_obs["sheets"] = W.dump_sheets(ev["cells"], ev["order"])
            wb_obs["format"] = "one line per non-empty cell: Sheet!A1 | formula (blank if constant) | value after LibreOffice recalculation of a copy"
        observations = {
            "stage": stage,
            "workbook_recalc": wb_obs,
            "workbook_value_hits": _trim_hits(hits),
            "file_inventory": inv,
            "docx_texts": docx_texts,
            "other_texts": other_texts,
            "pdf_texts": pdf_texts,
            "document_read_errors": text_errors,
            "expected_reference": {
                "source": "computed from trusted HarborPoint_Oct2026_SLA_Workbook.xlsx incidents, Exhibit C 1.4/4, "
                          "SRE review window CHG-24117 1:00-2:00 AM ET; these are expected values, not Worker content",
                **ref},
        }
    finally:
        if ev.get("tmpdir"):
            shutil.rmtree(ev["tmpdir"], ignore_errors=True)
    return {"checks": checks, "observations": observations}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--stage", required=True, choices=STAGES)
    args = ap.parse_args(argv)
    result = run(args.workspace, args.stage)
    json.dump(result, sys.stdout, ensure_ascii=False, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
