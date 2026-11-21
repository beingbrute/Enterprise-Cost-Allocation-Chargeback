"""
Export the reporting views (schema rpt) to CSV for the Excel control workpaper and Power BI.

Writes to reporting/:
  allocation_summary.csv, fact_chargeback.csv, journals.csv, exceptions.csv,
  control_results.csv, reconciliations.csv, monthly_kpis.csv,
  dim_period.csv, dim_pool.csv, dim_business_unit.csv, dim_cost_centre_period.csv
  control_totals.csv  - totals calculated in SQL, so Excel and Power BI can prove
                        they show the same numbers (reconciliations R09 and R10)

Usage:
    python src/export_reporting.py --port 5432
"""
import argparse
import csv
from pathlib import Path

from db import add_connection_args, connect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "reporting"

VIEWS = ["allocation_summary", "fact_chargeback", "journals", "exceptions", "control_results",
         "reconciliations", "monthly_kpis", "dim_period", "dim_pool", "dim_business_unit",
         "dim_cost_centre_period"]

CONTROL_TOTALS = [
    ("T01", "Total shared cost (source expense)", "SELECT SUM(pool_cost) FROM rpt.allocation_summary"),
    ("T02", "Total chargeback allocated (net)", "SELECT SUM(amount) FROM rpt.fact_chargeback"),
    ("T03", "Unallocated at close (all months)", "SELECT SUM(unallocated_at_close) FROM rpt.allocation_summary"),
    ("T04", "Chargeback BU01", "SELECT SUM(amount) FROM rpt.fact_chargeback WHERE bu_code = 'BU01'"),
    ("T05", "Chargeback BU02", "SELECT SUM(amount) FROM rpt.fact_chargeback WHERE bu_code = 'BU02'"),
    ("T06", "Chargeback BU03", "SELECT SUM(amount) FROM rpt.fact_chargeback WHERE bu_code = 'BU03'"),
    ("T07", "Chargeback BU04", "SELECT SUM(amount) FROM rpt.fact_chargeback WHERE bu_code = 'BU04'"),
    ("T08", "Chargeback BU05", "SELECT SUM(amount) FROM rpt.fact_chargeback WHERE bu_code = 'BU05'"),
    ("T09", "Posted journals", "SELECT COUNT(*) FROM rpt.journals WHERE status = 'Posted'"),
    ("T10", "Journal debits minus credits (all)", "SELECT SUM(difference) FROM rpt.journals"),
    ("T11", "Exceptions", "SELECT COUNT(*) FROM rpt.exceptions"),
    ("T12", "Exceptions escalated", "SELECT COUNT(*) FROM rpt.exceptions WHERE escalated"),
    ("T13", "Control results passed", "SELECT COUNT(*) FROM rpt.control_results WHERE passed"),
    ("T14", "Reconciliation failures", "SELECT COUNT(*) FROM rpt.reconciliations WHERE result = 'Fail'"),
]


def main():
    p = argparse.ArgumentParser()
    add_connection_args(p)
    args = p.parse_args()
    OUT.mkdir(exist_ok=True)

    con = connect(args)
    with con, con.cursor() as cur:
        for view in VIEWS:
            cur.execute(f"SELECT * FROM rpt.{view}")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            with open(OUT / f"{view}.csv", "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(cols)
                w.writerows(rows)
            print(f"  {view:<24} {len(rows):>6} rows")

        with open(OUT / "control_totals.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["total_id", "description", "sql_value"])
            for tid, desc, sql in CONTROL_TOTALS:
                cur.execute(sql)
                w.writerow([tid, desc, cur.fetchone()[0]])
        print(f"  {'control_totals':<24} {len(CONTROL_TOTALS):>6} rows")
    con.close()
    print(f"Exported to {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
