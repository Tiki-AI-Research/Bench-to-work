# fy26-merit-promotion-calibration: FY26 merit and promotion calibration: finish the workbook, absorb an HR Ops batch, executive approvals and a pre-lock update, then apply the CFO's Product decision and reissue the package

Multi-round benchmark task with 5 stages, 17 programmatic checks, and 14 rubric criteria.

## Background

It is November 20, 2026. You have the FY26 Compensation & Calibration Guidelines (v1.0, published October 27, 2026), Owen's calibration workbook with his status notes on the README tab, and the shell of the Executive Calibration Summary that goes to Elena and Robert. The workbook is the system of record for FY26 and Finance will audit it, so it has to stay formula-driven and traceable rather than carrying pasted values. The summary stays marked as a draft until you have reviewed it, and the current file names are fine for now; saving over the existing files or saving clearly labeled new versions are both acceptable. Items outside the Guidelines, such as market adjustments or pay below a band minimum, are handled outside this cycle: they should not add money to the package, though a short note is fine. The package is due December 4 and the roster locks December 1.

## Stages

| Stage | Title | Initial Mode | Attachments | Max Turns | Workspace | Programmatic Checks | Rubric Criteria |
|---|---|---|---|---:|---|---:|---:|
| s1_complete_baseline | Finish the workbook open items and populate the draft executive summary | fixed | FY26_Compensation_Calibration_Guidelines.pdf, FY26_Merit_Calibration_Workbook.xlsx, Executive_Calibration_Summary_FY26_DRAFT.docx | 3 | yes | 4 | 3 |
| s2_hr_ops_batch | Propagate the approved HR Ops personnel action batch through the workbook and summary | fixed | HR_Ops_Approved_Personnel_Actions_2026-11-20.pdf | 3 | yes | 4 | 2 |
| s3_approvals | Reconcile the executive approvals against the package and record approval status | fixed | FY26_Calibration_Approval_Record_2026-11-24.docx | 3 | yes | 1 | 2 |
| s4_final_package | Apply the pre-lock roster update and issue the final workbook and executive summary | fixed | HR_Ops_Pre_Lock_Roster_Update_2026-11-30.pdf | 3 | yes | 3 | 3 |
| s5_cfo_product_decision | Apply the CFO/CPO decision on Product merit spend and reissue the final package | fixed | FY26_Product_Merit_Decision_2026-12-07.docx | 3 | yes | 5 | 4 |

## Provenance & Validation

Benchmark v0.1 reference task (5-stage enterprise compensation calibration with live formula verification and gold reference solutions).

## Directory Layout

```text
task.toml        Harbor task configuration
instruction.md   Human/Harbor summary (not sent to the Worker)
environment/     Isolated Worker container context: Dockerfile, worker.py, public/ hook scripts
tests/test.sh    Harbor placeholder (evaluation runs on the controller side)
spec/            Controller-side specification: task.json, materials, verifier.py, verify/ (never mounted into container)
```
