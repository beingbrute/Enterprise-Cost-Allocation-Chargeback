# Design: Enterprise Cost Allocation, Chargeback Controls & BI Reporting

> **All data in this project is synthetic.** The company, cost centres, amounts and approvals are invented to demonstrate the process. Approvals and reviews are simulated by the pipeline and labelled as such. Journals are posted to a simulated ledger in the database; nothing is posted to a real ledger.

---

## 1. The business scenario

**Halden Group** (fictional) runs four shared services centrally. Their costs are first booked to central **pool cost centres**. Each month, Shared Services Finance charges the costs back to the business units that used the services, based on measurable drivers.

The monthly process this project reproduces:

```
Source files received (expenses, volumes, hierarchy, policies)
        ↓
Validation checks  ──fail──►  exception opened, affected pool blocked, owner assigned
        ↓ pass
Allocation (rates, shares, cent-level rounding)
        ↓
Journal prepared → validated → approved (simulated) → posted (simulated ledger)
        ↓
Reconciliations (10 checks)
        ↓
Month-close checklist (8 controls) → close approved only if no critical exception is open
        ↓
Reporting tables → Excel control workpaper → Power BI
```

**Period covered:** January to December 2025, run month by month in order. Currency: USD.

---

## 2. Organisation hierarchy

Three levels: **business unit → department → cost centre**.

| Business unit | Code | Departments |
|---|---|---|
| Consumer Banking | BU01 | D01 Retail Branches, D02 Cards, D03 Digital Channels |
| Commercial Banking | BU02 | D04 Corporate Lending, D05 Trade Finance |
| Wealth Management | BU03 | D06 Advisory, D07 Investment Products, D08 Private Clients |
| Operations | BU04 | D09 Payments Operations, D10 Customer Service |
| Corporate Functions | BU05 | D11 Finance, D12 HR & Legal |

- **About 60 recipient cost centres** (CC1001–CC1060), each belonging to one department.
- **4 pool cost centres** in a separate Shared Services unit (BU90), which are never recipients.
- Every mapping has **effective dates** (`valid_from`, `valid_to`) and an **active** flag, so a reorganisation applies only from its effective date and past months are reported under the structure that was true at the time.

**Planned hierarchy changes during 2025:**

| Effective | Change |
|---|---|
| 1 Apr 2025 | CC1023 moves from D04 Corporate Lending (BU02) to D09 Payments Operations (BU04) |
| 1 May 2025 | New cost centre CC1061 opens in D03 Digital Channels |
| 1 Jul 2025 | Department D10 Customer Service moves from BU04 to BU01 |
| 1 Sep 2025 | CC1047 closes (inactive; must receive no allocations from September) |
| 1 Oct 2025 | CC1012 renamed (metadata change only, no allocation impact) |

The hierarchy and the allocation policies are **versioned separately**: moving a department does not change which driver applies to it.

---

## 3. Cost pools, drivers and policies

| Pool | Pool cost centre | Source GL account | Driver | Business reason |
|---|---|---|---|---|
| P1 Software Licences | CC9101 | 6100 Software expense | Active licensed users | Cost follows licence consumption |
| P2 IT Service Desk | CC9102 | 6200 IT support expense | Support tickets | Support workload |
| P3 Shared Administration | CC9103 | 6300 Administration expense | Headcount | Broad employee-related support |
| P4 Office Facilities | CC9104 | 6400 Occupancy expense | Occupied floor area (sq ft) | Use of office space |

**Eligibility rules**
- Only cost centres **active** in the period receive allocations.
- **P4 Office Facilities:** fully remote cost centres (zero allocated floor area by policy) are **excluded**, not given a share.
- Pool cost centres never receive allocations.

**Policy versions (effective-dated)**

| Pool | Version | Effective | Driver |
|---|---|---|---|
| P2 IT Service Desk | v1 | Jan–Jun 2025 | Tickets **raised** |
| P2 IT Service Desk | v2 | from 1 Jul 2025 | Tickets **resolved** |
| All others | v1 | full year | as above |

---

## 4. Allocation method

For each pool and month:

```
rate              = pool cost ÷ total eligible driver units
allocated cost(i) = pool cost × eligible units(i) ÷ total eligible units
```

- **Exact decimal arithmetic**; amounts rounded to the cent with the **largest-remainder method**, so the allocations always add back to the pool cost exactly, to the cent.
- **Blocking, not guessing:** if a pool's inputs fail a blocking check (for example a missing volume for an active cost centre, an unknown cost centre or a negative value), that pool is **not allocated** for the month. Its cost stays in the pool as **unallocated** and an exception is opened. Missing data is never treated as zero.

---

## 5. Accounting treatment and journals

The original expense stays where it was booked (on the pool cost centre, accounts 6100–6400). The chargeback moves it with a balanced journal:

| Line | Cost centre | Account | Debit | Credit |
|---|---|---|---|---|
| One line per recipient | Recipient cost centre | 71x0 Allocated expense (7110 Software, 7120 IT support, 7130 Admin, 7140 Occupancy) | allocated amount | |
| One credit line | Pool cost centre | 7900 Allocation recovery (contra-expense) | | pool total allocated |

**Result:** each business unit shows its share of the cost, the pool cost centre nets to zero, and **company-wide expense is unchanged** (the debits on 71x0 and the credit on 7900 cancel out). This is checked every month.

**Journal controls**
- One journal per pool per month, ID such as `JE-2025-03-P2-V1`.
- **Status flow:** draft → validated → approved *(simulated reviewer)* → posted *(simulated ledger)*.
- A journal is posted only if: debits = credits, every account and cost centre is valid and active, the period is open, and that pool's reconciliations passed.
- **No duplicate postings:** re-running a month that is already posted creates nothing new (the key is period + pool + allocation version).
- **Corrections after posting** create a **reversal** (the original lines negated) and a **replacement** journal (version 2). History is never overwritten.
- Each month is closed after posting; a closed period accepts no new journals.

---

## 6. Validation checks (raise exceptions)

| Code | Check | Severity | Blocks the pool? |
|---|---|---|---|
| V01 | Expense file control total ≠ loaded total | Critical | Yes |
| V02 | Volume row for an unknown cost centre | High | Yes |
| V03 | Volume missing for an active, eligible cost centre | High | Yes |
| V04 | Negative volume | High | Yes |
| V05 | Duplicate volume rows | Medium | Yes, until removed |
| V06 | Volume for an inactive (closed) cost centre | Medium | Yes |
| V07 | Cost centre mapped to an unknown department | Critical | Yes |
| V08 | File received after the cut-off (working day 3) | Low | No (warning) |
| V09 | Zero total driver units for a pool | High | Yes |

---

## 7. Exceptions, SLAs and escalation

Each exception records: ID, run ID, period, check code, severity, pool, cost centre, description, **amount at risk**, owner, opened date, **due date**, status, root-cause code, resolution note and escalation history.

| Severity | SLA (working days) | Escalates when |
|---|---|---|
| Critical | 1 | Past due, or immediately if amount at risk > $50,000 |
| High | 3 | Past due |
| Medium | 5 | Past due |
| Low | 10 | Past due |

- **Status flow:** Open → Resolved (when a rerun passes the check) → Closed (at the next approved month close). **Reopened** if a later run fails the same check again.
- **Owners (roles):** Cost Centre Data Owner, Shared Services Finance, FP&A Reporting.
- **Root-cause codes:** RC01 source data error, RC02 late submission, RC03 hierarchy/mapping error, RC04 duplicate data, RC05 policy/configuration.
- Ageing is measured in **working days** (weekends and listed holidays excluded).

---

## 8. Reconciliations (every month)

| # | Reconciliation | Required result |
|---|---|---|
| R01 | Source expense file vs loaded expense | Exact match |
| R02 | Pool cost = allocated + unallocated | Exact match |
| R03 | Allocation detail vs allocation summary | Exact match |
| R04 | Journal debits vs credits | Zero difference |
| R05 | Posted journals vs allocation schedule | Exact match |
| R06 | Company-wide expense before vs after reallocation | Unchanged |
| R07 | Each cost centre maps to exactly one department and business unit for the period | No invalid or double mappings |
| R08 | No duplicate active posting for the same period, pool and version | Zero duplicates |
| R09 | SQL totals vs Excel workpaper | Exact match (checked in Stage 7) |
| R10 | SQL totals vs Power BI measures | Exact match (checked in Stage 8) |

A sample of allocations is also recalculated independently in pandas, so the tests do not just compare two outputs of the same SQL.

---

## 9. Monthly control checklist

| Control | Description | Evidence |
|---|---|---|
| C01 | All source files received | File manifest |
| C02 | Hierarchy valid for the period | Check output (V07, R07) |
| C03 | Volumes complete and valid | Exception report |
| C04 | Allocations complete or blocked with an exception | R02 output |
| C05 | Journals balanced and valid | R04 output, journal report |
| C06 | No duplicate postings | R08 output, run history |
| C07 | Ledger agrees with reporting | R05, R06 output |
| C08 | Month-close approval | Close checklist: approved only if no **critical** exception is open |

Every control result keeps its **run ID, timestamp, evidence reference, preparer (the pipeline) and reviewer status (simulated)**. Failed runs are kept, not overwritten.

---

## 10. Planned test scenarios (answer key kept separately)

Problems are planted in the synthetic data. The answer key lives in its own file and is used only by the tests.

| Month | Planted problem | Expected outcome |
|---|---|---|
| Feb | P1 expense control total does not match the loaded file | V01, P1 blocked |
| Mar | P2 volume row for unknown cost centre CC1099 | V02, P2 blocked |
| Apr | Hierarchy change: CC1023 moves business unit | Allocated to BU04 from April only |
| May | P1 volume missing for active CC1033 | V03, P1 blocked |
| Jun | P4 negative floor area for CC1018 | V04, P4 blocked |
| Jun | Correction: April P3 headcount for CC1015 was wrong | Reversal + replacement journal |
| Aug | P2 duplicate volume rows | V05, P2 blocked until removed |
| Sep–Dec | CC1047 closed | Receives no allocations |
| Oct | P3 volume file arrives late; P4 volume sent for closed CC1047 | V08 warning; V06, P4 blocked |
| Nov | Wrong mapping: CC1050 mapped to unknown department D99 | V07, blocked, corrected, re-run, reconciled |
| Any | Re-run of an already posted month | No new journals |

---

## 11. Build stages

| Stage | Output |
|---|---|
| 1 | This design document |
| 2 | `src/generate_data.py`: synthetic source files and the hidden answer key |
| 3 | PostgreSQL schema, effective-dated hierarchy and policies, validation checks |
| 4 | Allocation engine |
| 5 | Journals, simulated ledger, reconciliations, duplicate prevention, reversals |
| 6 | Exception workflow, escalation, month close, full-year run including the wrong-mapping scenario |
| 7 | Excel control workpaper (Power Query) |
| 8 | Power BI report (4 pages) |
| 9 | Acceptance tests, README, SOP, data dictionary, management summary, screenshots |

## 12. Out of scope

A real ERP or general ledger (SAP, Oracle), Anaplan, tax, intercompany transfer pricing and real approvals. This project shows the process and the controls, not experience inside a real finance department.
