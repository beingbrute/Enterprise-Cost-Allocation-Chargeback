# Management summary: shared services chargeback, FY2025

Halden Group (fictional company, synthetic data). Prepared from the reporting views and the Power BI report in this repository.

## Headline

- **$7,625,228.08** of shared service cost was posted in 2025 and **all of it was charged back** to the five business units by year end.
- **11 of 12 months** were fully allocated at month-end close. In June, **$203,890.15** of office facilities cost (2.7% of the year's cost) could not be allocated by close because the floor-area file had an error; it was charged in July.
- **All 12 month-end closes were approved.** 95 of 96 close controls passed; the one failure was June's volume control (C03), linked to the same office facilities issue.
- **98 journals** were posted, every one balanced. **490 of 491** reconciliation checks passed; the one failure was February's software expense file, which was blocked before posting and corrected the next day.

## Chargeback by business unit

| Business unit | 2025 chargeback | Share |
|---|---|---|
| Consumer Banking | $2,616,996.51 | 34.3% |
| Wealth Management | $1,641,673.28 | 21.5% |
| Operations | $1,359,097.32 | 17.8% |
| Commercial Banking | $1,055,171.21 | 13.8% |
| Corporate Functions | $952,289.76 | 12.5% |
| **Total** | **$7,625,228.08** | **100%** |

The largest movement in the year came from the organisation change, not from cost growth: when Customer Service moved from Operations to Consumer Banking on 1 July, Consumer Banking's monthly charge rose by $71,169 and Operations' fell by $63,318.

## Exceptions

| Measure | 2025 |
|---|---|
| Exceptions raised | 9 |
| Critical | 2 (February expense file, November cost centre mapping) |
| Escalated | 3 |
| Resolved within SLA | 8 of 9 |
| Open at year end | 0 |

The one SLA miss was the June floor-area error (High, 3 working days). The corrected file arrived on 14 July, 7 working days after it was raised. It was escalated on 9 July, the morning after it went past due.

Six of the nine exceptions came from errors in volume files (unknown, missing, negative, duplicate or closed cost centres). One file arrived after the cut-off.

## Points for attention

1. **Volume file quality.** Volume files caused most exceptions and the only month with unallocated cost at close. A pre-submission check by data owners (cost centre list for the month, no negatives, no duplicates) would catch these before the cut-off.
2. **Late corrections.** The June correction arrived after close, and the April headcount correction arrived two months late and needed a reversal and replacement. Corrections received after close should have a deadline in the next month.
3. **Hierarchy changes.** The November mapping error (a cost centre mapped to a department that did not exist) blocked all four pools until it was corrected the next day. Mapping changes should be checked against the department list before they are submitted.

## Expense policy review

All 484 expense claims paid in 2025 ($208,155.37) were reviewed against the travel and expense policy after payment.

| Measure | 2025 |
|---|---|
| Claims flagged by a policy check | 15 (3.1%) |
| Cleared after review | 1 (a taxi claim, once the missing receipt was provided) |
| Amount disallowed | $2,254.00 |
| Recovered | $2,164.00 (96.0%) |
| Outstanding at 30 Jan 2026 | $90.00 (one December case, recovery requested) |
| Cases escalated | 3 |
| Decided within deadline | 14 of 15 |
| Governance controls passed | 72 of 72 |

Most breaches were spending over a limit (hotel, meals, entertainment, upgrades and alcohol: 9 cases, partly reimbursed); one duplicate claim was rejected. Three were approval problems (a self-approved claim, a claim approved one level too low, and a purchase split across two claims to stay under the department-head limit); the spend itself was valid, so these were corrected by re-approval rather than disallowed. One employee had three cases in the year and was reported to their finance director.

**Point for attention:** approval breaches are the more serious finding, because they bypass a control rather than overspend a limit. The expense system should block self-approval and check the approval level against the combined total of claims from the same merchant on the same day.

## How the numbers were checked

- Reconciliations R01 to R08 after every chargeback run.
- R09: the Excel workpaper recalculates 18 SQL totals; all match.
- R10: the Power BI report recalculates 20 SQL totals; all match.
- 43 automated tests compare the results with two answer keys. Twelve recalculate allocations independently from the raw files, and one applies every expense policy rule to the raw claims with pandas and must find exactly the same breaches as the SQL checks.
