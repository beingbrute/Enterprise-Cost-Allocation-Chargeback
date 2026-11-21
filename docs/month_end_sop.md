# Month-end chargeback SOP

How the monthly shared-cost chargeback is run, as implemented in `src/pipeline.py`. Working days exclude weekends and the holidays in `data/raw/reference/holidays.csv`. Roles are those used in the exception log; reviewer approvals are simulated in this project.

## Calendar

| When | Step | Owner |
|---|---|---|
| Working days 1–3 | Data owners submit expense, volume and hierarchy files | Cost Centre Data Owner, Shared Services Finance |
| Working day 3, 18:00 | Cut-off run | Shared Services Finance |
| Every working day, 09:00 | Escalation check | Shared Services Finance |
| Until close | Rerun 1 hour after each corrected or late file | Shared Services Finance |
| Working day 6, 17:00 | Month close | FP&A Reporting (approval) |
| After close | Adjustments for files received late, posted in the next open month | Shared Services Finance |

## 1. Cut-off run (working day 3, 18:00)

1. Take the latest version of each file received by the cut-off (`ops.run_files` records which files were used).
2. Apply hierarchy change files. A file is applied in full or not at all, and the same request is never applied twice.
3. Validate each pool (checks V01 to V09 in `docs/design.md`). A failed blocking check:
   - opens an exception with severity, owner, root-cause code, amount at risk and due date,
   - blocks the pool for this run. The pool's cost stays unallocated. Missing values are not replaced with zero.
4. Post the source expense journal for each pool.
5. Allocate each pool that passed, using the policy (driver) in force for the month and only active, eligible cost centres. Office facilities excludes remote-only cost centres.
6. Create the allocation journal and take it through draft, validated, approved and posted. A journal posts only if debits equal credits, all accounts and cost centres are valid, and the period is open.
7. Run reconciliations R01 to R08 and save the results with the run ID.

## 2. Exceptions and escalation

- **SLA in working days:** Critical 1, High 3, Medium 5, Low 10. The due date is 17:00 on the last day.
- **Escalate immediately** if a Critical exception has more than $50,000 at risk.
- **Escalate at the 09:00 check** any exception that is past its due date and still open.
- The data owner sends a corrected file (a new version, for example `volumes_P2_v2.csv`). The original file is kept.
- An exception is resolved when a rerun no longer finds the problem. It is closed at the next approved month close. If the problem comes back, it is reopened.

## 3. Reruns (before close)

- Run one hour after each new or corrected file arrives.
- Pools already posted show **Already posted** and are not posted again. The journal idempotency key (period, pool, allocation version) and the input fingerprint prevent duplicates.
- Blocked pools are validated again and, if they pass, allocated and posted.

## 4. Month close (working day 6, 17:00)

Record each control with run ID, evidence reference, preparer and reviewer status:

| Control | Passes when |
|---|---|
| C01 | All 8 expense and volume files received |
| C02 | No open mapping exception (V07) and R07 passed |
| C03 | No open volume exception (V02–V06, V09) |
| C04 | Every pool is allocated or has an open exception explaining why not |
| C05 | R04 passed and no journal left unposted |
| C06 | R08 passed (no duplicate postings) |
| C07 | R05 and R06 passed |
| C08 | Close approved: no Critical exception open and C02, C05, C06, C07 all passed |

If C08 is approved, the period is closed and accepts no new journals. If it is rejected, fix the cause and run the close again. Control results from failed attempts are kept.

A pool can still be unallocated at an approved close if its exception is not Critical (June 2025). The unallocated amount is reported and caught up when the corrected file arrives.

## 5. After close

- **Catch-up:** a corrected file for a closed month is allocated and posted in the next open month. The journal keeps the original month as its source period.
- **Correction of a posted month:** post a reversal of the original journal and a replacement journal (version 2) in the next open month. Do not change or delete the original.

## 6. Reporting

1. `python src/export_reporting.py` exports the reporting views and the SQL control totals.
2. Refresh the Excel workpaper (Data > Refresh All) and check that R09 shows OK for all 12 totals.
3. Refresh the Power BI report and check that R10 shows OK for all 14 totals.
4. Review unallocated cost at close, open and escalated exceptions, and failed controls before sending the report.
