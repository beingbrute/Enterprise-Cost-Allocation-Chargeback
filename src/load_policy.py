"""
Load the expense policy module into the chargeback database (schema pol).

Runs sql/04, 05 and 06 (policy schema, checks and reporting views). They rebuild schema pol and its
views only; the chargeback year is not touched. Then loads data/raw/policy/*.csv as received.
Nothing is checked here: the checks and cases are made by the monthly review (src/policy_review.py).

Usage:
    python src/load_policy.py --port 5432
"""
import argparse
import csv
import sys
from pathlib import Path

from db import add_connection_args, connect

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "policy"
SQL = ROOT / "sql"


def rows(name):
    with open(RAW / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def blank(v):
    return v if v not in ("", None) else None


def main():
    p = argparse.ArgumentParser()
    add_connection_args(p)
    args = p.parse_args()
    if not (RAW / "claims.csv").exists():
        print("ERROR: data/raw/policy not found. Run: python src/generate_policy_data.py")
        sys.exit(2)

    con = connect(args)
    with con, con.cursor() as cur:
        for f in ("04_policy_schema.sql", "05_policy_functions.sql", "06_policy_views.sql"):
            cur.execute((SQL / f).read_text(encoding="utf-8"))

        emps = rows("employees.csv")
        for e in emps:      # insert first, link approvers second (they refer to each other)
            cur.execute("INSERT INTO pol.employees (employee_id, cc_code, dept_code, bu_code, role) VALUES (%s,%s,%s,%s,%s)",
                        (e["employee_id"], e["cc_code"], e["dept_code"], e["bu_code"], e["role"]))
        for e in emps:
            cur.execute("UPDATE pol.employees SET line_manager_id=%s, dept_head_id=%s, finance_director_id=%s "
                        "WHERE employee_id=%s",
                        (blank(e["line_manager_id"]), e["dept_head_id"], e["finance_director_id"], e["employee_id"]))
        for r in rows("policy_rules.csv"):
            cur.execute("INSERT INTO pol.policy_rules VALUES (%s,%s,%s,%s,%s)",
                        (r["version"], r["valid_from"], r["valid_to"], r["rule_key"], r["value"]))
        for r in rows("high_cost_cities.csv"):
            cur.execute("INSERT INTO pol.high_cost_cities VALUES (%s)", (r["city"],))
        for level, r in enumerate(rows("approval_matrix.csv"), start=1):
            cur.execute("INSERT INTO pol.approval_matrix VALUES (%s,%s,%s,%s)",
                        (level, r["min_total"], r["max_total"], r["approver_role"]))
        for r in rows("exception_register.csv"):
            cur.execute("INSERT INTO pol.exception_register (exception_id, employee_id, rule_code, reason, approved_by, "
                        "valid_from, valid_to, source) VALUES (%s,%s,%s,%s,%s,%s,%s,'Pre-approved')",
                        (r["exception_id"], r["employee_id"], r["rule_code"], r["reason"], r["approved_by"],
                         blank(r["valid_from"]), blank(r["valid_to"])))
        claims = rows("claims.csv")
        for r in claims:
            cur.execute("INSERT INTO pol.claims VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (r["claim_id"], r["employee_id"], r["submitted_date"], r["approved_by"], r["approved_date"],
                         r["paid_date"], r["claim_total"], blank(r["business_purpose"])))
        lines = rows("claim_lines.csv")
        for r in lines:
            cur.execute("INSERT INTO pol.claim_lines VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (r["line_id"], r["claim_id"], r["expense_date"], r["category"], r["merchant"],
                         blank(r["city"]), blank(r["nights"]), blank(r["attendees"]), r["amount"],
                         blank(r["receipt_id"]), blank(r["travel_class"]), r["alcohol"] == "True",
                         r["client_present"] == "True", blank(r["pre_approval_ref"])))
        actions = rows("reviewer_actions.csv")
        for r in actions:
            cur.execute("INSERT INTO pol.reviewer_actions VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (r["claim_id"], r["check_code"], r["decision"], r["disallowed"], r["triage_after_wd"],
                         r["decide_after_wd"], r["evidence"], blank(r["recover_after_wd"]),
                         blank(r["recovery_result"]), blank(r["retry_after_wd"]), blank(r["reviewer_note"])))
        cur.execute("SELECT SUM(claim_total) FROM pol.claims")
        total = cur.fetchone()[0]
    con.close()

    print(f"Policy module: {len(emps)} employees, {len(claims)} claims ({len(lines)} lines, ${total:,.2f}), "
          f"{len(actions)} reviewer actions")
    print("Done. Next: python src/policy_review.py run-year")


if __name__ == "__main__":
    main()
