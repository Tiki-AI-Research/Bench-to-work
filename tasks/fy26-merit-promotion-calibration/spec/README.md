# FY26 Merit and Promotion Calibration (`fy26-merit-promotion-calibration`)

Five-stage enterprise compensation calibration workflow supporting Megan Lowell (Director of Total Rewards at Larkspur Software, Inc.):

1. **`s1_complete_baseline`**: Complete `Calc` Band Max cap / lump-sum split (`N:Q`), `Dept_Pools`, and `Company Promotion Pool` via live formulas; populate `Executive_Calibration_Summary_FY26_DRAFT.docx` while preserving `DRAFT` status; flag below-band-minimum L1 salaries without unauthorized adjustments; log updates on `Change_Log`.
2. **`s2_hr_ops_batch`**: Incorporate `HR_Ops_Approved_Personnel_Actions_2026-11-20.pdf` (three pre-lock FY26 actions applied, one Jan 4, 2027 transfer deferred to FY27) through live formulas while preserving draft status.
3. **`s3_approvals`**: Reconcile `FY26_Calibration_Approval_Record_2026-11-24.docx` (CFO `$2,500` conditional department ceiling, CPO promotion-pool approval, VP lump-sum sign-offs > `$3,000`) against post-batch figures.
4. **`s4_final_package`**: Apply `HR_Ops_Pre_Lock_Roster_Update_2026-11-30.pdf` (`E2007` rating `3 -> 4`), strip `DRAFT` to issue the December 2, 2026 final package, and report both resulting CFO gaps (`$943.75` Product above ceiling and `$314.79` Company Total over budget blocking G&A).
5. **`s5_cfo_product_decision`**: Apply `FY26_Product_Merit_Decision_2026-12-07.docx` (uniform proportional factor `0.9740914862` on Product merit percentages so Product recurring merit lands at `$44,482.50` while preserving `E2006` Band Max capping with lump sum `$295.00 -> $54.18` and keeping 8% promotions and non-Product departments untouched), document the one-time exception, and reissue the December 8, 2026 final package.

## Running Standalone Verification

```bash
python3 -B verifier.py --workspace <submission_dir> --stage <stage_id>
```
