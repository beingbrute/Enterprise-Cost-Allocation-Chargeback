"""
Generate the synthetic source files for the chargeback project (Halden Group, Jan-Dec 2025).

All data is invented. The script is deterministic (fixed seed), so every run produces the
same files.

Output (data/raw/):
  reference/   business_units, departments, cost_centres (as at 1 Jan 2025), cost_pools,
               gl_accounts, allocation_policies, holidays
  2025-MM/     expenses_<pool>.csv, volumes_<pool>.csv, hierarchy_changes.csv (if any),
               manifest.csv (one row per file, with row count, control total and received time)
               Corrected resubmissions use the suffix _v2 and arrive later.

Answer key (data/answer_key/expected_exceptions.csv) lists every planted problem and what
the pipeline should do with it. Only the tests read it; the pipeline never does.

Usage:
    python src/generate_data.py
"""
import csv
import random
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
KEY = ROOT / "data" / "answer_key"
SEED = 2025
YEAR = 2025
CENT = Decimal("0.01")

# --------------------------------------------------------------------------- reference data
BUSINESS_UNITS = [
    ("BU01", "Consumer Banking"), ("BU02", "Commercial Banking"), ("BU03", "Wealth Management"),
    ("BU04", "Operations"), ("BU05", "Corporate Functions"), ("BU90", "Shared Services"),
]
DEPARTMENTS = [  # code, name, business unit as at 1 Jan 2025, number of cost centres
    ("D01", "Retail Branches", "BU01", 7), ("D02", "Cards", "BU01", 5), ("D03", "Digital Channels", "BU01", 5),
    ("D04", "Corporate Lending", "BU02", 6), ("D05", "Trade Finance", "BU02", 4),
    ("D06", "Advisory", "BU03", 5), ("D07", "Investment Products", "BU03", 5), ("D08", "Private Clients", "BU03", 4),
    ("D09", "Payments Operations", "BU04", 6), ("D10", "Customer Service", "BU04", 6),
    ("D11", "Finance", "BU05", 4), ("D12", "HR & Legal", "BU05", 3),
    ("D90", "Shared Services", "BU90", 0),
]
COST_POOLS = [  # pool, name, pool cost centre, source account, allocated account, driver v1, monthly budget
    ("P1", "Software Licences", "CC9101", "6100", "7110", "licensed_users", 182000),
    ("P2", "IT Service Desk", "CC9102", "6200", "7120", "tickets_raised", 96000),
    ("P3", "Shared Administration", "CC9103", "6300", "7130", "headcount", 141000),
    ("P4", "Office Facilities", "CC9104", "6400", "7140", "floor_area_sqft", 214000),
]
GL_ACCOUNTS = [
    ("2100", "Accounts payable (simulated)", "Liability"),
    ("6100", "Software expense", "Source expense"), ("6200", "IT support expense", "Source expense"),
    ("6300", "Administration expense", "Source expense"), ("6400", "Occupancy expense", "Source expense"),
    ("7110", "Allocated software expense", "Allocated expense"),
    ("7120", "Allocated IT support expense", "Allocated expense"),
    ("7130", "Allocated administration expense", "Allocated expense"),
    ("7140", "Allocated occupancy expense", "Allocated expense"),
    ("7900", "Allocation recovery", "Contra expense"),
]
HOLIDAYS_2025_2026 = [
    ("2025-01-01", "New Year's Day"), ("2025-04-18", "Good Friday"), ("2025-04-21", "Easter Monday"),
    ("2025-05-05", "Early May bank holiday"), ("2025-05-26", "Spring bank holiday"),
    ("2025-08-25", "Summer bank holiday"), ("2025-12-25", "Christmas Day"), ("2025-12-26", "Boxing Day"),
    ("2026-01-01", "New Year's Day"),
]
REMOTE_ONLY = {"CC1014", "CC1027", "CC1036", "CC1044", "CC1052", "CC1058"}  # no office space: excluded from P4

rng = random.Random(SEED)


def money(x):
    return Decimal(str(x)).quantize(CENT, rounding=ROUND_HALF_UP)


def write_csv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


# --------------------------------------------------------------------------- calendar helpers
HOLIDAY_DATES = {date.fromisoformat(d) for d, _ in HOLIDAYS_2025_2026}


def working_day(year, month, n):
    """The n-th working day of a month (weekends and holidays excluded)."""
    d, count = date(year, month, 1), 0
    while True:
        if d.weekday() < 5 and d not in HOLIDAY_DATES:
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)


def next_month(period):
    y, m = period
    return (y + 1, 1) if m == 12 else (y, m + 1)


def stamp(d, hour, minute=0):
    return datetime(d.year, d.month, d.day, hour, minute).isoformat(sep=" ")


# --------------------------------------------------------------------------- organisation
def build_cost_centres():
    """Cost centres as at 1 Jan 2025: code, name, department, remote_only, base size."""
    centres, n = [], 1001
    for dept, dname, _bu, count in DEPARTMENTS:
        for i in range(count):
            code = f"CC{n}"
            headcount = rng.randint(12, 140)
            centres.append({"code": code, "name": f"{dname} {i + 1:02d}", "dept": dept,
                            "remote": code in REMOTE_ONLY, "headcount": headcount})
            n += 1
    return centres


def hierarchy_changes():
    """Change requests received during the year, keyed by the reporting month they arrive with."""
    return {
        4: [("HCR-2025-004", "MOVE", "CC1023", "D09", "", "", "2025-04-01")],
        5: [("HCR-2025-005", "OPEN", "CC1061", "D03", "", "Digital Channels 06", "2025-05-01")],
        7: [("HCR-2025-007", "DEPT_MOVE", "D10", "", "BU01", "", "2025-07-01")],
        9: [("HCR-2025-009", "CLOSE", "CC1047", "", "", "", "2025-09-01")],
        10: [("HCR-2025-010", "RENAME", "CC1012", "", "", "Cards Premium 02", "2025-10-01")],
        # Planted error: D99 does not exist (intended D08). The corrected file follows as _v2.
        11: [("HCR-2025-011", "MOVE", "CC1050", "D99", "", "", "2025-11-01")],
    }


# --------------------------------------------------------------------------- monthly files
def active_centres(centres, month):
    out = []
    for c in centres:
        if c["code"] == "CC1047" and month >= 9:
            continue
        if c["code"] == "CC1061" and month < 5:
            continue
        out.append(c)
    return out


def volumes_for(pool, c, month):
    """Driver quantities for one cost centre in one month."""
    hc = c["headcount"] + rng.randint(-3, 3)
    if pool == "P1":
        return [("licensed_users", max(1, round(hc * rng.uniform(1.05, 1.35))))]
    if pool == "P2":
        raised = max(0, round(hc * rng.uniform(0.25, 0.70)))
        resolved = max(0, raised - rng.randint(0, max(1, raised // 8)))
        return [("tickets_raised", raised), ("tickets_resolved", resolved)]
    if pool == "P3":
        return [("headcount", max(1, hc))]
    if pool == "P4":
        return [] if c["remote"] else [("floor_area_sqft", max(200, round(hc * rng.uniform(95, 135))))]
    raise ValueError(pool)


def expense_lines(pool, budget, month):
    """3-8 invoice/accrual lines that add up to roughly the monthly budget."""
    total = budget * rng.uniform(0.92, 1.10) * (1.04 if month in (11, 12) else 1.0)
    n = rng.randint(3, 8)
    weights = [rng.uniform(0.5, 1.5) for _ in range(n)]
    s = sum(weights)
    amounts = [money(total * w / s) for w in weights]
    return amounts


def main():
    centres = build_cost_centres()
    pools = {p[0]: p for p in COST_POOLS}

    # ---- reference files
    ref = RAW / "reference"
    write_csv(ref / "business_units.csv", ["bu_code", "bu_name"], BUSINESS_UNITS)
    write_csv(ref / "departments.csv", ["dept_code", "dept_name", "bu_code", "valid_from"],
              [(d, n, b, "2025-01-01") for d, n, b, _ in DEPARTMENTS])
    write_csv(ref / "cost_centres.csv", ["cc_code", "cc_name", "dept_code", "remote_only", "valid_from"],
              [(c["code"], c["name"], c["dept"], c["remote"], "2025-01-01") for c in centres]
              + [(p[2], p[1] + " Pool", "D90", False, "2025-01-01") for p in COST_POOLS])
    write_csv(ref / "cost_pools.csv",
              ["pool_code", "pool_name", "pool_cc", "source_account", "allocated_account", "recovery_account"],
              [(p[0], p[1], p[2], p[3], p[4], "7900") for p in COST_POOLS])
    write_csv(ref / "gl_accounts.csv", ["account", "account_name", "account_type"], GL_ACCOUNTS)
    write_csv(ref / "allocation_policies.csv",
              ["pool_code", "version", "driver", "excludes_remote_only", "valid_from", "valid_to"],
              [("P1", 1, "licensed_users", False, "2025-01-01", "9999-12-31"),
               ("P2", 1, "tickets_raised", False, "2025-01-01", "2025-06-30"),
               ("P2", 2, "tickets_resolved", False, "2025-07-01", "9999-12-31"),
               ("P3", 1, "headcount", False, "2025-01-01", "9999-12-31"),
               ("P4", 1, "floor_area_sqft", True, "2025-01-01", "9999-12-31")])
    write_csv(ref / "holidays.csv", ["holiday_date", "holiday_name"], HOLIDAYS_2025_2026)

    changes = hierarchy_changes()
    key = []  # answer key rows

    # CC1061 opens in May: give it a size now so volumes can be generated
    cc1061 = {"code": "CC1061", "name": "Digital Channels 06", "dept": "D03", "remote": False, "headcount": 38}
    all_centres = centres + [cc1061]

    for month in range(1, 13):
        period = f"{YEAR}-{month:02d}"
        folder = RAW / period
        ny, nm = next_month((YEAR, month))
        wd = lambda n: working_day(ny, nm, n)
        manifest = []
        active = active_centres(all_centres, month)

        # ---- hierarchy change file (arrives working day 1)
        if month in changes:
            rows = changes[month]
            write_csv(folder / "hierarchy_changes.csv",
                      ["request_id", "change_type", "object_code", "new_dept", "new_bu", "new_name", "effective_date"], rows)
            manifest.append(("hierarchy_changes.csv", "", "HIERARCHY", len(rows), "", stamp(wd(1), 10)))
            if month == 11:
                fixed = [("HCR-2025-011", "MOVE", "CC1050", "D08", "", "", "2025-11-01")]
                write_csv(folder / "hierarchy_changes_v2.csv",
                          ["request_id", "change_type", "object_code", "new_dept", "new_bu", "new_name", "effective_date"], fixed)
                manifest.append(("hierarchy_changes_v2.csv", "", "HIERARCHY", 1, "", stamp(wd(4), 11)))
                key.append((period, "V07", "ALL", "CC1050", "Critical", "blocked", "hierarchy_changes.csv",
                            "Mapped to unknown department D99; corrected to D08 in v2"))

        for pool_code, pool_name, pool_cc, src_acct, _alloc, _drv, budget in COST_POOLS:
            # ---- expenses
            amounts = expense_lines(pool_code, budget, month)
            exp_rows = [(period, pool_code, pool_cc, src_acct, f"INV-{period}-{pool_code}-{i + 1:02d}", a)
                        for i, a in enumerate(amounts)]
            control_total = sum(amounts)
            exp_header = ["period", "pool_code", "pool_cc", "account", "source_ref", "amount"]
            if pool_code == "P1" and month == 2:
                # Planted: one invoice line missing from the file, so it does not match the control total
                write_csv(folder / "expenses_P1.csv", exp_header, exp_rows[:-1])
                manifest.append(("expenses_P1.csv", pool_code, "EXPENSE", len(exp_rows) - 1, control_total, stamp(wd(2), 9)))
                write_csv(folder / "expenses_P1_v2.csv", exp_header, exp_rows)
                manifest.append(("expenses_P1_v2.csv", pool_code, "EXPENSE", len(exp_rows), control_total, stamp(wd(4), 15)))
                key.append((period, "V01", pool_code, "", "Critical", "blocked", "expenses_P1.csv",
                            f"Missing line {exp_rows[-1][4]} ({exp_rows[-1][5]}); corrected in v2"))
            else:
                write_csv(folder / f"expenses_{pool_code}.csv", exp_header, exp_rows)
                manifest.append((f"expenses_{pool_code}.csv", pool_code, "EXPENSE", len(exp_rows), control_total, stamp(wd(2), 9)))

            # ---- volumes
            vol_header = ["period", "pool_code", "cc_code", "driver", "quantity"]
            vol_rows = []
            for c in active:
                for driver, qty in volumes_for(pool_code, c, month):
                    vol_rows.append([period, pool_code, c["code"], driver, qty])
            good = [list(r) for r in vol_rows]
            bad = None
            received = stamp(wd(3), 12)
            fixed_received = None

            if pool_code == "P2" and month == 3:
                bad = good + [[period, "P2", "CC1099", "tickets_raised", 41], [period, "P2", "CC1099", "tickets_resolved", 39]]
                key.append((period, "V02", "P2", "CC1099", "High", "blocked", "volumes_P2.csv", "Unknown cost centre; removed in v2"))
            if pool_code == "P1" and month == 5:
                bad = [r for r in good if r[2] != "CC1033"]
                key.append((period, "V03", "P1", "CC1033", "High", "blocked", "volumes_P1.csv", "Volume missing for active cost centre; added in v2"))
            if pool_code == "P4" and month == 6:
                bad = [list(r) for r in good]
                for r in bad:
                    if r[2] == "CC1018":
                        r[4] = -r[4]
                key.append((period, "V04", "P4", "CC1018", "High", "blocked_at_close", "volumes_P4.csv",
                            "Negative floor area; corrected file arrives after June close, allocated as a July catch-up"))
            if pool_code == "P2" and month == 8:
                dups = [r for r in good if r[2] in ("CC1005", "CC1029")]
                bad = good + [list(r) for r in dups]
                key.append((period, "V05", "P2", "CC1005;CC1029", "Medium", "blocked", "volumes_P2.csv", "4 duplicate rows; removed in v2"))
            if pool_code == "P3" and month == 10:
                received = stamp(wd(5), 16)   # late: after the working-day-3 cut-off
                key.append((period, "V08", "P3", "", "Low", "warning", "volumes_P3.csv", "File received after cut-off"))
            if pool_code == "P4" and month == 10:
                bad = good + [[period, "P4", "CC1047", "floor_area_sqft", 3150]]
                key.append((period, "V06", "P4", "CC1047", "Medium", "blocked", "volumes_P4.csv", "Volume for closed cost centre; removed in v2"))

            vol_file = f"volumes_{pool_code}.csv"
            if bad is not None:
                write_csv(folder / vol_file, vol_header, bad)
                manifest.append((vol_file, pool_code, "VOLUME", len(bad), sum(r[4] for r in bad), received))
                v2_day = wd(10) if (pool_code == "P4" and month == 6) else wd(4)
                write_csv(folder / f"volumes_{pool_code}_v2.csv", vol_header, good)
                manifest.append((f"volumes_{pool_code}_v2.csv", pool_code, "VOLUME", len(good), sum(r[4] for r in good), stamp(v2_day, 14)))
            else:
                write_csv(folder / vol_file, vol_header, good)
                manifest.append((vol_file, pool_code, "VOLUME", len(good), sum(r[4] for r in good), received))

        write_csv(folder / "manifest.csv",
                  ["file_name", "pool_code", "file_type", "row_count", "control_total", "received_at"], manifest)

    # ---- correction after posting: April P3 headcount for CC1015 was overstated; corrected file sent in June
    apr = RAW / "2025-04"
    with open(apr / "volumes_P3.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    header, body = rows[0], [list(r) for r in rows[1:]]
    for r in body:
        if r[2] == "CC1015":
            original = int(r[4])
            r[4] = str(original - 20)
    write_csv(apr / "volumes_P3_correction.csv", header, body)
    with open(apr / "manifest.csv", newline="", encoding="utf-8") as f:
        m = list(csv.reader(f))
    m.append(["volumes_P3_correction.csv", "P3", "VOLUME", str(len(body)), str(sum(int(r[4]) for r in body)),
              stamp(working_day(2025, 6, 8), 10)])
    write_csv(apr / "manifest.csv", m[0], m[1:])
    key.append(("2025-04", "CORRECTION", "P3", "CC1015", "", "reversal_and_replacement", "volumes_P3_correction.csv",
                f"Headcount {original} corrected to {original - 20}; received June, reversal and replacement journals in June"))

    key.append(("2025-09..2025-12", "INACTIVE", "ALL", "CC1047", "", "no_allocation", "", "Closed from 1 Sep 2025"))
    key.append(("2025-04..2025-12", "HIERARCHY", "ALL", "CC1023", "", "allocated_to_BU04", "", "Moved to D09 (BU04) from 1 Apr 2025"))
    key.append(("2025-07..2025-12", "HIERARCHY", "ALL", "D10", "", "allocated_to_BU01", "", "Department moved to BU01 from 1 Jul 2025"))
    key.append(("2025-07..2025-12", "POLICY", "P2", "", "", "driver_tickets_resolved", "", "P2 policy v2 from 1 Jul 2025"))

    write_csv(KEY / "expected_exceptions.csv",
              ["period", "check_code", "pool_code", "cc_code", "severity", "expected_outcome", "file_name", "note"], key)
    n_files = sum(1 for _ in RAW.rglob("*.csv"))
    print(f"Wrote {n_files} files to {RAW.relative_to(ROOT)} and {len(key)} answer-key rows to {KEY.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
