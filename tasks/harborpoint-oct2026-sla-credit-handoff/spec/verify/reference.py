"""Independent reference values derived from the trusted originals.

The trusted originals live beside verifier.py under their task-relative
paths.  Missing or unreadable trusted material is an environment error and is
raised, never converted into a Worker failure.
"""
import calendar
import datetime as _dt
from pathlib import Path

import openpyxl

CODE_DIR = Path(__file__).resolve().parent.parent
TRUSTED_WORKBOOK_REL = "HarborPoint_SLA_Credit/HarborPoint_Oct2026_SLA_Workbook.xlsx"

# Trusted originals published to the Worker over the task (task-relative paths).
TRUSTED_FILES = [
    "HarborPoint_SLA_Credit/Contract/MSA_Exhibit_C_Service_Levels.pdf",
    "HarborPoint_SLA_Credit/Correspondence/2026-11-10_Ack_to_Greg_DRAFT.docx",
    "HarborPoint_SLA_Credit/Correspondence/2026-11-25_Greg_Ostrowski_Notice_Dispute.pdf",
    "HarborPoint_SLA_Credit/Correspondence/HP-4452_Inbound_Customer_Emails.pdf",
    "HarborPoint_SLA_Credit/Dana_Handover_Notes_2026-11-13.docx",
    "HarborPoint_SLA_Credit/HarborPoint_Oct2026_SLA_Workbook.xlsx",
    "HarborPoint_SLA_Credit/Internal/2026-11-19_Marcus_Lee_RE_Approval_Request.pdf",
    "HarborPoint_SLA_Credit/Internal/2026-11-23_SRE_Maintenance_Overlap_Review.pdf",
    "HarborPoint_SLA_Credit/Internal/2026-11-30_Marcus_Lee_Decision.pdf",
    "HarborPoint_SLA_Credit/Policies/Service_Credit_Approval_SOP_CS-SOP-014.pdf",
]

# Facts read from the trusted PDFs while writing this verifier (not parsed at
# run time):
#  * Exhibit C 1.4: Total Minutes = calendar days in Service Month x 1,440;
#    1.5: uptime rounded to two decimals; section 4 credit bands; Order Form
#    OF-HPL-2025-03 Monthly Subscription Fee USD 48,000.00.
#  * SRE review (2026-11-23): CHG-24117 announced window Thu Oct 15
#    1:00 AM - 2:00 AM ET; INC-77821 unavailable 12:52 AM - 3:12 AM.
SERVICE_YEAR, SERVICE_MONTH = 2026, 10
CHG_24117_WINDOW = (_dt.time(1, 0), _dt.time(2, 0))
EXHIBIT_C_FEE = 48000
SRE_INCIDENT_TICKET = "HP-4431"


def _minutes(t):
    return t.hour * 60 + t.minute + t.second / 60.0


def _tier_for(uptime_pct_2dp, tiers):
    """tiers: list of (min_fraction_inclusive, credit_fraction), descending."""
    frac = uptime_pct_2dp / 100.0
    for lo, credit in tiers:
        if frac >= lo - 1e-12:
            return credit
    raise RuntimeError("credit tier table does not cover uptime %r" % uptime_pct_2dp)


def compute_reference():
    path = CODE_DIR / TRUSTED_WORKBOOK_REL
    wb = openpyxl.load_workbook(path)  # raises if trusted material is missing
    inc = wb["Incidents"]
    header = [c.value for c in inc[1]]
    col = {name: i for i, name in enumerate(header)}
    counted = 0
    sre_incident = None
    for row in inc.iter_rows(min_row=2, values_only=True):
        if not row or not isinstance(row[col["Ticket"]], str):
            continue
        start, end = row[col["Start (ET)"]], row[col["End (ET)"]]
        dur = round(_minutes(end) - _minutes(start))
        if row[col["Counted in SLA?"]] == "Yes":
            counted += dur
        if row[col["Ticket"]] == SRE_INCIDENT_TICKET:
            sre_incident = (start, end)
    if sre_incident is None:
        raise RuntimeError("trusted workbook lacks incident %s" % SRE_INCIDENT_TICKET)
    w0, w1 = (_minutes(t) for t in CHG_24117_WINDOW)
    s0, s1 = (_minutes(t) for t in sre_incident)
    in_window = round(max(0.0, min(s1, w1) - max(s0, w0)))

    calc = wb["Uptime_Calc"]
    as_received_total = calc["B4"].value
    fee = calc["B8"].value
    if fee != EXHIBIT_C_FEE:
        raise RuntimeError("trusted workbook fee %r differs from Order Form %r" % (fee, EXHIBIT_C_FEE))
    tiers = []
    ct = wb["Credit_Tiers"]
    for lo, credit, *_ in ct.iter_rows(min_row=2, values_only=True):
        if isinstance(lo, (int, float)) and isinstance(credit, (int, float)):
            tiers.append((float(lo), float(credit)))
    tiers.sort(reverse=True)

    total = calendar.monthrange(SERVICE_YEAR, SERVICE_MONTH)[1] * 1440

    def scenario(downtime, total_minutes):
        exact = (total_minutes - downtime) / total_minutes
        pct2 = round(exact * 100, 2)
        tier = _tier_for(pct2, tiers)
        return {
            "downtime_min": downtime,
            "total_minutes": total_minutes,
            "uptime_exact": exact,
            "uptime_fraction_4dp": round(exact, 4),
            "uptime_pct_2dp": pct2,
            "tier_fraction": tier,
            "credit": round(fee * tier, 2),
        }

    return {
        "total_minutes": total,
        "as_received_total_minutes": as_received_total,
        "fee": fee,
        "sre_in_window_minutes": in_window,
        "all_counted": scenario(counted, total),
        "excluded": scenario(counted - in_window, total),
        "as_received": scenario(counted, as_received_total),
    }
