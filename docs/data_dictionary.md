# Data dictionary: reporting views (schema `rpt`)

Excel and Power BI read only these views, defined in `sql/03_reporting_views.sql` (chargeback) and `sql/06_policy_views.sql` (expense policy). They are built from posted journals and recorded results, not from draft data. `src/export_reporting.py` writes each view to `reporting/<view>.csv`.

Periods are text in the form `YYYY-MM`. Amounts are USD with 2 decimals.

## Dimensions

**dim_period**: one row per month.
| Column | Meaning |
|---|---|
| period | Month, e.g. 2025-06 |
| period_start, period_end | First and last calendar day |
| month_no | 1 to 12 |
| period_label | Display label, e.g. Jan 2025 |
| status | Open or Closed |
| closed_at | When the month-close was approved |

**dim_pool**: one row per cost pool.
| Column | Meaning |
|---|---|
| pool_code | P1 Software Licences, P2 IT Service Desk, P3 Shared Administration, P4 Office Facilities |
| pool_name | Pool name |
| pool_cc | Pool cost centre the cost is booked to (CC9101–CC9104) |
| source_account | Expense account (6100–6400) |
| allocated_account | Allocated expense account used in chargeback journals (7110–7140) |

**dim_business_unit**: bu_code (BU01–BU05), bu_name.

**dim_cost_centre_period**: the hierarchy as it was in each month (one row per cost centre per month).
| Column | Meaning |
|---|---|
| period | Month |
| cc_code, cc_name | Cost centre |
| dept_code, dept_name | Department in that month |
| bu_code, bu_name | Business unit in that month |
| remote_only | True if the cost centre has no office space (excluded from P4) |
| is_active | False after a cost centre closes |

## Facts

**fact_chargeback**: one row per posted chargeback line (2,944 rows for 2025).
| Column | Meaning |
|---|---|
| ledger_id | Ledger line |
| journal_id | Journal the line belongs to |
| journal_type | Allocation, Replacement, Reversal or Catch-up |
| pool_code | Pool charged |
| source_period | Month the cost belongs to |
| posting_period | Month the journal was posted (differs for catch-ups and corrections) |
| cc_code, dept_code, bu_code | Receiving cost centre and its department and business unit in the source month |
| account | Allocated expense account (7110–7140) |
| amount | Debit minus credit. Reversal lines are negative. |

**allocation_summary**: one row per month and pool.
| Column | Meaning |
|---|---|
| period, pool_code, pool_name | Month and pool |
| driver | Driver in force (e.g. tickets_raised until June, tickets_resolved from July for P2) |
| total_units | Total eligible driver units |
| rate | pool_cost ÷ total_units |
| allocation_version | 1, or 2 after a correction |
| pool_cost | Source expense posted for the month |
| allocated_total | Chargeback posted for the month, net of reversals, at any time |
| unallocated_now | pool_cost − allocated_total |
| allocated_by_close | Chargeback posted by the time the month closed |
| unallocated_at_close | Cost still unallocated at month-end close |
| status_at_close | Pool status in the close run |

**journals**: one row per journal.
| Column | Meaning |
|---|---|
| journal_id | e.g. JE-2025-03-P2-V1, SRC-2025-03-P2, REV-JE-2025-04-P3-V1 |
| journal_type | Source, Allocation, Reversal, Replacement or Catch-up |
| source_period, posting_period | Month the cost belongs to and month posted |
| pool_code | Pool |
| status | Posted for all journals at year end |
| reverses_journal | For reversals, the journal being reversed |
| created_at, posted_at | Timestamps |
| approved_by | Finance reviewer (simulated) |
| total_debit, total_credit, difference | Line totals; difference is 0 for every journal |
| line_count | Number of lines |

**exceptions**: one row per exception.
| Column | Meaning |
|---|---|
| exception_id | ID |
| period | Month the problem belongs to |
| check_code, check_name | Validation check (V01–V09) |
| severity | Critical, High, Medium or Low |
| pool_code, cc_code | Pool and cost centre affected (blank if all) |
| owner_role | Role responsible for fixing it |
| root_cause | Root-cause code (RC01–RC05) |
| description | What was found |
| amount_at_risk | Cost that could not be allocated because of the problem (blank for warnings) |
| status | Open, Resolved, Closed or Reopened |
| escalated | True if escalated |
| opened_at, due_at, resolved_at, closed_at | Timeline |
| working_days_to_resolve | Working days from opened to resolved |
| resolved_within_sla | True if resolved by due_at |
| escalated_at | When it was escalated |

**control_results**: one row per control per close attempt.
| Column | Meaning |
|---|---|
| period, control_id, control_name | Month and control (C01–C08) |
| result | Pass, Fail, Approved or Rejected |
| passed | True for Pass or Approved |
| evidence_ref | Where the evidence is, with run ID |
| detail | Reason for a failure |
| prepared_by, reviewer_status | Pipeline; Reviewed (simulated) |
| recorded_at, run_id | When and in which run |

**reconciliations**: one row per reconciliation check per run (491 rows for 2025).
| Column | Meaning |
|---|---|
| run_id, run_type, run_at | Run that performed the check |
| period, recon_id, recon_name | Month and check (R01–R08) |
| pool_code | Pool, where the check is per pool |
| left_value, right_value, difference | The two values compared and the difference |
| result | Pass or Fail |

**monthly_kpis**: one row per month: shared_cost, allocated_by_close, unallocated_at_close, exceptions_opened, exceptions_escalated, controls_passed, controls_total, recon_failures, runs (excluding closes), status, closed_at.

## Expense policy views

**dim_employee**: employee_id, role (Staff, Manager, Department head, Finance director), cc_code, dept_code, bu_code, bu_name, line_manager_id, dept_head_id, finance_director_id.

**policy_claims**: one row per claim (484).
| Column | Meaning |
|---|---|
| claim_id, employee_id, bu_code | Claim and claimant |
| period | Month the claim was paid (the month it is reviewed) |
| submitted_date, approved_by, paid_date | Submission, approver, payment |
| claim_total | Total claimed |
| line_count | Number of lines |
| case_count | Cases raised on this claim |

**policy_cases**: one row per case (15).
| Column | Meaning |
|---|---|
| case_id, period, claim_id | Case, review month and claim |
| check_code, check_name, severity | Policy check (P01–P10) that failed |
| employee_id, bu_code | Claimant |
| amount_in_question | Amount the check found over the limit or at issue |
| detail | What the check found, with the policy version |
| owner_role, status | Owner; Open, Triaged, Decided, Remediation, Closed or Reopened |
| decision | No breach, Exception approved, Partly reimbursed, Rejected or Corrected |
| disallowed, recovered, outstanding | Amount disallowed, recovered so far, and the difference |
| evidence | Evidence recorded with the decision |
| escalated, escalated_at | Whether and when the case was escalated |
| opened_at, triaged_at, decision_due_at, decided_at, remediation_due_at, closed_at | Timeline |
| working_days_to_decide, decided_within_sla | Working days from intake to decision, and whether the deadline was met |
| reopened | True if a recovery failed |
| repeat_reported | True if this case made the employee a repeat case (3 in 12 months) |

**policy_case_events**: case_id, event_at, event_type (Opened, Triaged, Escalated, Decided, Exception recorded, Recovery requested, Recovery failed, Recovered, Closed, Repeat reported), detail.

**policy_governance**: one row per control per month (72): period, control_id (G01–G06), control_name, result (Pass or Fail), passed, evidence_ref, detail, prepared_by, reviewer_status, recorded_at.

**policy_exception_register**: exception_id, employee_id, rule_code, reason, approved_by, valid_from, valid_to, source (Pre-approved or Review decision), case_id, recorded_at.

**policy_monthly**: one row per month: claims_paid, amount_paid, claims_checked, cases_opened, cases_escalated, disallowed, recovered, controls_passed, controls_total.

## Control totals

`reporting/control_totals.csv` holds 20 totals calculated in SQL: T01–T14 for the chargeback (total cost, total chargeback, unallocated at close, chargeback per business unit, posted journals, journal difference, exceptions, escalations, controls passed, reconciliation failures) and T15–T20 for the expense policy review (claims checked, cases, escalated, disallowed, recovered, governance controls passed). The Excel workpaper (R09) recalculates 18 of them and the Power BI report (R10) all 20.
