"""
Create the `chargeback` database, build the schema and load every source file as received.

  1. creates the database if it does not exist
  2. runs sql/01_schema.sql, sql/02_functions.sql and sql/03_reporting_views.sql
  3. loads reference data (the hierarchy as at 1 Jan 2025) and the 12 reporting periods
  4. loads every monthly file listed in each manifest into src.*, with its stated row count,
     control total and received time. Nothing is validated or corrected here: that happens
     in the monthly runs (src/pipeline.py).

Usage:
    python src/load_sources.py --port 5432
"""
import argparse
import calendar
import csv
import sys
from pathlib import Path

from db import DB_NAME, add_connection_args, connect

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
SQL = ROOT / "sql"


def rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def file_version(name):
    if "_correction" in name:
        return 3
    if "_v2" in name:
        return 2
    return 1


def main():
    p = argparse.ArgumentParser()
    add_connection_args(p)
    args = p.parse_args()

    if not (RAW / "reference" / "cost_centres.csv").exists():
        print("ERROR: data/raw not found. Run: python src/generate_data.py")
        sys.exit(2)

    con = connect(args, "postgres")
    con.autocommit = True
    with con.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
        if cur.fetchone() is None:
            cur.execute(f"CREATE DATABASE {DB_NAME}")
            print(f"Created database {DB_NAME}")
    con.close()

    con = connect(args)
    with con, con.cursor() as cur:
        for f in ("01_schema.sql", "02_functions.sql", "03_reporting_views.sql"):
            cur.execute((SQL / f).read_text(encoding="utf-8"))

        ref = RAW / "reference"
        for r in rows(ref / "business_units.csv"):
            cur.execute("INSERT INTO ref.business_units VALUES (%s, %s)", (r["bu_code"], r["bu_name"]))
        for r in rows(ref / "departments.csv"):
            cur.execute("INSERT INTO ref.departments (dept_code, dept_name, bu_code, valid_from) VALUES (%s,%s,%s,%s)",
                        (r["dept_code"], r["dept_name"], r["bu_code"], r["valid_from"]))
        for r in rows(ref / "cost_centres.csv"):
            cur.execute("INSERT INTO ref.cost_centres (cc_code, cc_name, dept_code, remote_only, valid_from) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (r["cc_code"], r["cc_name"], r["dept_code"], r["remote_only"] == "True", r["valid_from"]))
        for r in rows(ref / "gl_accounts.csv"):
            cur.execute("INSERT INTO ref.gl_accounts VALUES (%s,%s,%s)", (r["account"], r["account_name"], r["account_type"]))
        for r in rows(ref / "cost_pools.csv"):
            cur.execute("INSERT INTO ref.cost_pools VALUES (%s,%s,%s,%s,%s,%s)",
                        (r["pool_code"], r["pool_name"], r["pool_cc"], r["source_account"],
                         r["allocated_account"], r["recovery_account"]))
        for r in rows(ref / "allocation_policies.csv"):
            cur.execute("INSERT INTO ref.allocation_policies VALUES (%s,%s,%s,%s,%s,%s)",
                        (r["pool_code"], int(r["version"]), r["driver"], r["excludes_remote_only"] == "True",
                         r["valid_from"], r["valid_to"]))
        for r in rows(ref / "holidays.csv"):
            cur.execute("INSERT INTO ref.holidays VALUES (%s,%s)", (r["holiday_date"], r["holiday_name"]))

        counts = {"files": 0, "expense": 0, "volume": 0, "hierarchy": 0}
        for folder in sorted(RAW.glob("2025-*")):
            period = folder.name
            y, m = map(int, period.split("-"))
            cur.execute("INSERT INTO ref.periods VALUES (%s, %s, %s, 'Open', NULL)",
                        (period, f"{period}-01", f"{period}-{calendar.monthrange(y, m)[1]:02d}"))

        for folder in sorted(RAW.glob("2025-*")):
            period = folder.name
            for mf in rows(folder / "manifest.csv"):
                name = mf["file_name"]
                cur.execute(
                    "INSERT INTO src.files (period, file_name, pool_code, file_type, version, row_count, "
                    "control_total, received_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING file_id",
                    (period, name, mf["pool_code"] or None, mf["file_type"], file_version(name),
                     int(mf["row_count"]), mf["control_total"] or None, mf["received_at"]))
                fid = cur.fetchone()[0]
                counts["files"] += 1
                data = rows(folder / name)
                for i, r in enumerate(data, start=1):
                    if mf["file_type"] == "EXPENSE":
                        cur.execute("INSERT INTO src.expense_lines VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                                    (fid, i, r["period"], r["pool_code"], r["pool_cc"], r["account"],
                                     r["source_ref"], r["amount"]))
                        counts["expense"] += 1
                    elif mf["file_type"] == "VOLUME":
                        cur.execute("INSERT INTO src.volume_lines VALUES (%s,%s,%s,%s,%s,%s,%s)",
                                    (fid, i, r["period"], r["pool_code"], r["cc_code"], r["driver"], r["quantity"]))
                        counts["volume"] += 1
                    else:
                        cur.execute("INSERT INTO src.hierarchy_changes VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                    (fid, i, r["request_id"], r["change_type"], r["object_code"],
                                     r["new_dept"] or None, r["new_bu"] or None, r["new_name"] or None,
                                     r["effective_date"]))
                        counts["hierarchy"] += 1

        cur.execute("SELECT COUNT(*) FROM ref.cost_centres")
        n_cc = cur.fetchone()[0]
    con.close()

    print(f"Reference: {n_cc} cost centres, 12 periods")
    print(f"Source files: {counts['files']} files, {counts['expense']} expense lines, "
          f"{counts['volume']} volume lines, {counts['hierarchy']} hierarchy changes")
    print("Done. Next: python src/pipeline.py run-year")


if __name__ == "__main__":
    main()
