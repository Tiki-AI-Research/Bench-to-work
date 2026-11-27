# Harbor Point Logistics: October 2026 SLA Credit Handoff (`harborpoint-oct2026-sla-credit-handoff`)

Multi-round office workflow task in which the assistant supports Jordan Ellis (Senior Customer Success Manager, Arcadia Ledger, Inc.) across four stages (`s1_inherited_claim` through `s4_decision_closeout`) to audit an inherited SLA credit claim (`HP-4452`), reconcile contractual uptime and maintenance-overlap calculations, maintain accurate case-ledger and customer-communication history, escalate an exclusion-notice dispute under SOP `CS-SOP-014`, and prepare the final close-out package (`Credit Memo Request` and final customer confirmation).

## Package Structure

- `task.json` — Four-stage `Bench-to-Work` task specification, user simulator instructions, semantic evaluation `criteria`, and programmatic `verifier_checks`.
- `verifier.py` & `verify/` — Stage-aware programmatic verifier and observation collector (`workbook.py`, `reference.py`, `texts.py`).
- `HarborPoint_SLA_Credit/` — Trusted public materials (`MSA_Exhibit_C_Service_Levels.pdf`, `Service_Credit_Approval_SOP_CS-SOP-014.pdf`, `HarborPoint_Oct2026_SLA_Workbook.xlsx`, correspondence, handover notes, SRE review, and Director decision).
- `manifest.json` — SHA-256 file integrity manifest.

## Programmatic Checks (`verifier_checks`)

- `s1_inherited_claim` -> `s1_workbook_basis_corrected`: Verifies that the SLA workbook computes October 2026 uptime (`99.47%` / `0.9947`), credit tier (`10%`), and credit (`$4,800.00`) via live formulas driven by the 31-day minute base (`44,640`), with no active `99.45%` (`43,200`-minute) formula result (allowing side audit/reconciliation variance formulas against legacy `43,200`).
- `s2_sre_overlap` -> `s2_workbook_exclusion_scenario`: Verifies the 60-minute `CHG-24117` overlap exclusion calculation (`176` counted minutes, `99.61%` uptime, `5%` tier, `$2,400.00` credit).
- `s3_customer_dispute` -> `s3_workbook_both_calcs`: Verifies that both calculations (`236` min / `99.47%` / `10%` / `$4,800.00` and `176` min / `99.61%` / `5%` / `$2,400.00`) are retained via live formulas pending Director ruling.
- `s4_decision_closeout` -> `s4_workbook_final_result`: Verifies that the close-out workbook computes the approved final primary SLA result (`44,640` minute base, `236` counted downtime minutes, `99.47%` uptime, `10%` tier, `$4,800.00` credit) via live formulas (allowing clearly retained historical comparison scenarios) and emits full `observations` for Reviewer criteria.

## Running Verification

```bash
python3 -B verifier.py --workspace <submission_dir> --stage <stage_id>
```
