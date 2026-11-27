# harborpoint-oct2026-sla-credit-handoff: Harbor Point Logistics: October 2026 SLA credit claim, from inherited case file to approved credit

Multi-round benchmark task with 4 stages, 4 programmatic checks, and 14 rubric criteria.

## Background

As of Monday morning, November 23, 2026, you support Jordan Ellis (Senior Customer Success Manager at Arcadia Ledger, Inc.), who took over the Harbor Point Logistics account (`ARC-10482`) from Dana Whitfield while Dana is out on Thanksgiving-week PTO without mailbox access. Harbor Point's Corporate Controller, Greg Ostrowski, filed an October 2026 SLA credit claim (`HP-4452`) on November 3. Across four consecutive stages, the assistant audits Dana's inherited workbook and correspondence (catching the 30-day `43,200` vs. October 31-day `44,640` minute denominator error and recognizing that Dana's November 10 acknowledgment already reached the customer), folds in SRE's `CHG-24117` maintenance-overlap review, handles the customer's contractual notice-contact dispute before the holiday without over-committing on unapproved credits or billing cutoffs, and closes out the case record, Billing Credit Memo Request, and final customer confirmation after the Director's ruling. Stages 2–4 use `workspace`-aware dynamic User Simulator turns and Stage 3 includes conditional intra-stage `continuations`.

## Stages

| Stage | Title | Initial Mode | Attachments | Max Turns | Workspace | Programmatic Checks | Rubric Criteria |
|---|---|---|---|---:|---|---:|---:|
| s1_inherited_claim | Review the inherited claim file, update the record, draft the customer reply and approval note | fixed | MSA_Exhibit_C_Service_Levels.pdf, Service_Credit_Approval_SOP_CS-SOP-014.pdf, HarborPoint_Oct2026_SLA_Workbook.xlsx, HP-4452_Inbound_Customer_Emails.pdf, 2026-11-10_Ack_to_Greg_DRAFT.docx, Dana_Handover_Notes_2026-11-13.docx, 2026-11-19_Marcus_Lee_RE_Approval_Request.pdf | 3 | yes | 1 | 4 |
| s2_sre_overlap | Fold in SRE's maintenance-overlap review and re-route the credit | workspace | 2026-11-23_SRE_Maintenance_Overlap_Review.pdf | 3 | yes | 1 | 3 |
| s3_customer_dispute | Handle the customer's dispute of the maintenance exclusion before the holiday | workspace | 2026-11-25_Greg_Ostrowski_Notice_Dispute.pdf | 3 | yes | 1 | 3 |
| s4_decision_closeout | Close out after the Director's decision: records, billing request, and final customer note | workspace | 2026-11-30_Marcus_Lee_Decision.pdf | 3 | yes | 1 | 4 |

## Provenance & Validation

Benchmark v0.1 reference task (4-stage enterprise SLA credit handoff with live formula verification, `workspace`-aware User Simulator state transitions, and gold reference solutions).

## Directory Layout

```text
task.toml        Harbor task configuration
instruction.md   Harbor single-turn loader summary placeholder (multi-turn prompts are sent dynamically by the controller)
environment/     Isolated Worker container context: Dockerfile, worker.py, public/ (optional hook scripts; empty in this task)
tests/test.sh    Harbor single-turn loader placeholder (evaluation runs on the controller side)
spec/            Controller-side specification: task.json, HarborPoint_SLA_Credit/, verifier.py, verify/ (never mounted into container)
```
