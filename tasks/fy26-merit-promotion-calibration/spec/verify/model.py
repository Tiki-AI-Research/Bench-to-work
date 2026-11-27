"""Independent reference model for the FY26 merit / promotion calibration.

Rules are taken from the trusted public PDFs (facts extracted while writing the
verifier, not parsed at run time):
  Guidelines §3 eligibility/proration, §4 merit matrix, §5 promotion 8% / pool 0.6%,
  §6 promotion-first Band Max cap with lump-sum conversion, §7 department rates
  and lock-date assignment, §8 lump-sum threshold 3,000, §9 bands.
  HR Ops batch 2026-11-20 (PA-26-1187/1194/1203/1211), pre-lock update 2026-11-30
  (E2007 rating 3->4), Product decision 2026-12-07 (uniform factor, target budget+2,500).
The roster itself is read from the trusted workbook placed beside the verifier.
"""
import datetime as _dt
from pathlib import Path

import openpyxl

FY_START = _dt.date(2026, 1, 1)
HIRE_CUTOFF = _dt.date(2026, 9, 30)
PROMO_RATE = 0.08
PROMO_POOL_RATE = 0.006
LUMP_APPROVAL_THRESHOLD = 3000.0
CFO_CEILING = 2500.0
MERIT_MATRIX = {5: 0.055, 4: 0.04, 3: 0.0275, 2: 0.01, 1: 0.0}
BAND_MAX = {"L1": 95000, "L2": 118000, "L3": 145000, "L4": 178000, "L5": 218000, "L6": 265000}
BAND_MIN = {"L1": 70000, "L2": 85000, "L3": 105000, "L4": 130000, "L5": 160000, "L6": 195000}
DEPT_RATES = {"Engineering": 0.0375, "Product": 0.035, "Sales": 0.0325,
              "Customer Success": 0.0325, "G&A": 0.03}
DEPTS = list(DEPT_RATES)

TRUSTED_WORKBOOK = "FY26_Merit_Calibration_Workbook.xlsx"
TRUSTED_SUMMARY = "Executive_Calibration_Summary_FY26_DRAFT.docx"
ROSTER_HEADERS = {
    "id": "Employee ID", "name": "Employee Name", "dept": "Department", "level": "Level",
    "hire": "Hire Date", "base": "Base Salary (USD)", "rating": "Performance Rating",
    "promo": "Promotion Approved (Y/N)", "promo_level": "Promotion Level",
    "leave_type": "Leave Type", "leave_months": "Leave Months (full)",
}

STAGES = ["s1_complete_baseline", "s2_hr_ops_batch", "s3_approvals",
          "s4_final_package", "s5_cfo_product_decision"]


def load_trusted_roster(code_dir):
    """Read the trusted Roster tab. Missing/corrupt trusted material raises."""
    wb = openpyxl.load_workbook(Path(code_dir) / TRUSTED_WORKBOOK, data_only=True)
    ws = wb["Roster"]
    rows = list(ws.iter_rows(values_only=True))
    header = list(rows[0])
    idx = {k: header.index(v) for k, v in ROSTER_HEADERS.items()}
    roster = []
    for r in rows[1:]:
        if r[idx["id"]] is None:
            continue
        hire = r[idx["hire"]]
        if isinstance(hire, _dt.datetime):
            hire = hire.date()
        roster.append({
            "id": str(r[idx["id"]]).strip(), "name": r[idx["name"]], "dept": r[idx["dept"]],
            "level": r[idx["level"]], "hire": hire, "base": float(r[idx["base"]]),
            "rating": int(r[idx["rating"]]), "promo": (r[idx["promo"]] or "N").strip().upper() == "Y",
            "promo_level": r[idx["promo_level"]], "leave_type": r[idx["leave_type"]] or "None",
            "leave_months": int(r[idx["leave_months"]] or 0),
        })
    if len(roster) != 38 or len({e["id"] for e in roster}) != 38:
        raise RuntimeError("trusted roster does not hold 38 unique employees")
    return roster


def stage_roster(base_roster, stage):
    """Apply the publicly delivered roster changes that are in force at `stage`."""
    k = STAGES.index(stage)
    roster = [dict(e) for e in base_roster]
    by = {e["id"]: e for e in roster}
    if k >= 1:  # HR Ops batch 2026-11-20
        by["E1009"]["dept"] = "Product"                      # PA-26-1187 eff. 2026-11-02
        by["E1012"]["promo"] = True                          # PA-26-1194 eff. 2026-11-15
        by["E1012"]["promo_level"] = "L4"
        by["E4002"]["leave_type"] = "Paid Parental"          # PA-26-1203 recode
        # PA-26-1211 (E3001, eff. 2027-01-04) is after the Dec 1 lock -> FY27, no change
    if k >= 3:  # pre-lock update 2026-11-30
        by["E2007"]["rating"] = 4
    return roster


def eligible_months(e):
    hire = e["hire"]
    if hire > HIRE_CUTOFF:
        return 0
    if hire <= FY_START:
        m = 12
    else:
        start = hire.month if hire.day == 1 else hire.month + 1
        m = 13 - start
    if e["leave_type"] == "Unpaid LOA":
        m -= e["leave_months"]
    return max(0, m)


def employee_amounts(e, merit_factor=1.0):
    months = eligible_months(e)
    pror = months / 12.0
    eff_level = e["promo_level"] if e["promo"] else e["level"]
    bmax = float(BAND_MAX[eff_level])
    rate = MERIT_MATRIX[e["rating"]]
    merit = e["base"] * rate * merit_factor * pror
    promo = e["base"] * PROMO_RATE if (e["promo"] and months > 0) else 0.0
    room = max(0.0, bmax - e["base"])
    rec_promo = min(promo, room)
    rec_merit = min(merit, max(0.0, room - rec_promo))
    lump = promo + merit - rec_promo - rec_merit
    new_base = e["base"] + rec_promo + rec_merit
    return {
        "id": e["id"], "name": e["name"], "dept": e["dept"], "level": e["level"],
        "effective_level": eff_level, "base": e["base"], "rating": e["rating"],
        "eligible_months": months, "proration": pror, "merit_pct": rate * merit_factor,
        "computed_merit": merit, "promotion": promo, "band_max": bmax,
        "rec_promo": rec_promo, "rec_merit": rec_merit, "lump": lump, "new_base": new_base,
    }


def pools(emps):
    out = {}
    for d in DEPTS:
        members = [x for x in emps if x["dept"] == d and x["eligible_months"] > 0]
        budget = DEPT_RATES[d] * sum(x["base"] * x["proration"] for x in members)
        spend = sum(x["rec_merit"] for x in emps if x["dept"] == d)
        out[d] = {"headcount": len(members), "budget": budget, "spend": spend,
                  "remaining": budget - spend, "over": spend > budget}
    cb = sum(v["budget"] for v in out.values())
    cs = sum(v["spend"] for v in out.values())
    out["Company Total"] = {"headcount": sum(v["headcount"] for v in out.values()),
                            "budget": cb, "spend": cs, "remaining": cb - cs, "over": cs > cb}
    pb = PROMO_POOL_RATE * sum(x["base"] for x in emps if x["eligible_months"] > 0)
    ps = sum(x["rec_promo"] for x in emps)
    promo = {"budget": pb, "spend": ps, "remaining": pb - ps, "over": ps > pb,
             "count": sum(1 for x in emps if x["promotion"] > 0)}
    lumps = [{"id": x["id"], "name": x["name"], "dept": x["dept"], "lump": x["lump"],
              "needs_vp": x["lump"] > LUMP_APPROVAL_THRESHOLD}
             for x in emps if x["lump"] > 1e-9]
    return out, promo, lumps


def solve_product_factor(roster):
    """Uniform factor on Product merit % so Product recurring spend = budget + 2,500."""
    def product_spend(f):
        emps = [employee_amounts(e, f if e["dept"] == "Product" else 1.0) for e in roster]
        p, _, _ = pools(emps)
        return p["Product"]["spend"], p["Product"]["budget"]
    _, budget = product_spend(1.0)
    target = budget + CFO_CEILING
    lo, hi = 0.0, 1.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if product_spend(mid)[0] < target:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2, target


def reference(code_dir, stage, product_factor=None):
    """Reference state at `stage`. product_factor overrides the solved s5 factor (used to
    recompute a workbook's own rounded factor)."""
    roster = stage_roster(load_trusted_roster(code_dir), stage)
    factor, target = None, None
    if stage == "s5_cfo_product_decision":
        factor, target = solve_product_factor(roster)
        if product_factor is not None:
            factor = product_factor
    emps = [employee_amounts(e, factor if (factor is not None and e["dept"] == "Product") else 1.0)
            for e in roster]
    dept, promo, lumps = pools(emps)
    return {
        "stage": stage, "employees": {x["id"]: x for x in emps}, "order": [x["id"] for x in emps],
        "dept": dept, "promo": promo, "lumps": lumps, "lump_total": sum(l["lump"] for l in lumps),
        "product_factor": factor, "product_target": target,
    }
