"""Acceptance tests for the expense policy module: the finished review year must match the answer key
and the rules in docs/expense_policy.md. Run after:
    python src/load_policy.py
    python src/policy_review.py run-year
"""
import csv
from decimal import Decimal

import pandas as pd
import pytest

from conftest import ROOT

KEY = ROOT / "data" / "answer_key" / "expected_policy_cases.csv"
RAW = ROOT / "data" / "raw" / "policy"


def answer_key():
    with open(KEY, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ------------------------------------------------------------------ cases against the answer key
def test_cases_match_answer_key(cur):
    expected = {(r["period"], r["claim_id"], r["check_code"], r["severity"], r["expected_decision"],
                 Decimal(r["disallowed"]), r["escalated"] == "True")
                for r in answer_key() if r["check_code"]}
    cur.execute("SELECT period, claim_id, check_code, severity, decision, disallowed, escalated FROM pol.cases")
    assert set(cur.fetchall()) == expected


def test_traps_raise_no_case(cur):
    """Claims that look close to a breach but are within policy: v1's 90-day deadline, v2's higher hotel cap,
    a high-cost city, and business class covered by a valid exception."""
    traps = [r["claim_id"] for r in answer_key() if not r["check_code"]]
    assert len(traps) == 4
    cur.execute("SELECT claim_id FROM pol.cases WHERE claim_id = ANY(%s)", (traps,))
    assert cur.fetchall() == []


def test_every_claim_checked_once(one):
    assert one("SELECT SUM(claims_checked) FROM pol.review_runs") == one("SELECT COUNT(*) FROM pol.claims") == 484
    assert one("SELECT COUNT(DISTINCT period) FROM pol.review_runs") == 12


def test_policy_version_follows_expense_date(cur):
    """A $220 hotel night on 26 Jun (paid in July) is checked against v1's $200 cap;
    the same rate on 8 Jul is within v2's $230 cap."""
    cur.execute("""SELECT k.claim_id, r.policy_version FROM pol.cases k
                   JOIN pol.check_results r ON r.claim_id = k.claim_id AND r.check_code = k.check_code
                   WHERE k.check_code = 'P02' AND k.period = '2025-07'""")
    rows = cur.fetchall()
    assert len(rows) == 1 and rows[0][1] == 1


# ------------------------------------------------------------------ case lifecycle
def test_no_case_closed_without_evidence_or_full_recovery(one):
    assert one("""SELECT COUNT(*) FROM pol.cases WHERE status = 'Closed'
                  AND (evidence IS NULL OR evidence = '' OR recovered < disallowed)""") == 0


def test_only_open_case_is_december_recovery(cur):
    cur.execute("SELECT period, check_code, status, disallowed - recovered FROM pol.cases WHERE status <> 'Closed'")
    assert cur.fetchall() == [("2025-12", "P05", "Remediation", Decimal("90.00"))]


def test_failed_recovery_reopens_then_closes(cur):
    cur.execute("""SELECT e.event_type, MIN(e.event_at) FROM pol.case_events e JOIN pol.cases k USING (case_id)
                   WHERE k.period = '2025-11' AND k.check_code = 'P04' GROUP BY e.event_type""")
    at = dict(cur.fetchall())
    assert at["Recovery failed"] < at["Recovered"] <= at["Closed"]
    assert at["Closed"].month == 12      # second attempt succeeded in December


def test_escalations(cur, one):
    # High cases over $1,000 at intake; the late October decision at the 09:00 check the day after its due date
    cur.execute("""SELECT k.check_code, k.period, MIN(e.event_at) = k.opened_at
                   FROM pol.cases k JOIN pol.case_events e USING (case_id)
                   WHERE e.event_type = 'Escalated' GROUP BY k.case_id ORDER BY k.period""")
    assert cur.fetchall() == [("P08", "2025-07", True), ("P08", "2025-09", True), ("P02", "2025-10", False)]
    assert one("""SELECT MIN(e.event_at)::TEXT FROM pol.case_events e JOIN pol.cases k USING (case_id)
                  WHERE k.period = '2025-10' AND e.event_type = 'Escalated'""") == "2025-11-12 09:00:00"


def test_repeat_offender_reported_once(cur):
    cur.execute("SELECT employee_id, case_count FROM pol.repeat_reports")
    rows = cur.fetchall()
    assert len(rows) == 1 and rows[0][1] == 3


def test_exception_decision_added_to_register(cur):
    cur.execute("SELECT rule_code, source FROM pol.exception_register WHERE source = 'Review decision'")
    assert cur.fetchall() == [("P09", "Review decision")]


def test_amounts(one):
    assert one("SELECT SUM(disallowed) FROM pol.cases") == Decimal("2254.00")
    assert one("SELECT SUM(recovered) FROM pol.cases") == Decimal("2164.00")


def test_all_governance_controls_passed(one):
    assert one("SELECT COUNT(*) FROM pol.governance_results") == 72
    assert one("SELECT COUNT(*) FROM pol.governance_results WHERE result <> 'Pass'") == 0


# ------------------------------------------------------------------ independent recalculation
def test_pandas_recalculation_finds_the_same_breaches(cur):
    """Apply the policy rules to the raw CSV files with pandas (no SQL) and compare the breaches found."""
    claims = pd.read_csv(RAW / "claims.csv", parse_dates=["submitted_date", "paid_date"])
    lines = pd.read_csv(RAW / "claim_lines.csv", parse_dates=["expense_date"]).merge(claims, on="claim_id")
    emp = pd.read_csv(RAW / "employees.csv").set_index("employee_id")
    # dates kept as ISO text: "9999-12-31" (open-ended) is outside the range pandas timestamps can hold
    rules = pd.read_csv(RAW / "policy_rules.csv", dtype=str)
    high = set(pd.read_csv(RAW / "high_cost_cities.csv").city)
    exc = pd.read_csv(RAW / "exception_register.csv", dtype=str)

    def rule(key, d):
        d = d.strftime("%Y-%m-%d")
        r = rules[(rules.rule_key == key) & (rules.valid_from <= d) & (rules.valid_to >= d)]
        return float(r.value.iloc[0])

    found = set()
    for _, r in lines.iterrows():
        d = r.expense_date
        if r.amount >= rule("receipt_min", d) and pd.isna(r.receipt_id):
            found.add((r.claim_id, "P01"))
        if r.category == "Hotel":
            cap = rule("hotel_high_cost_night" if r.city in high else "hotel_standard_night", d)
            if r.amount > cap * r.nights:
                found.add((r.claim_id, "P02"))
        if r.category == "Airfare" and r.travel_class != "Economy":
            ok = exc[(exc.exception_id == r.pre_approval_ref) & (exc.employee_id == r.employee_id)
                     & (exc.valid_from <= d.strftime("%Y-%m-%d")) & (exc.valid_to >= d.strftime("%Y-%m-%d"))]
            if ok.empty:
                found.add((r.claim_id, "P04"))
        if r.category == "Client entertainment" and (
                not r.client_present or r.amount > r.attendees * rule("entertainment_per_attendee", d)):
            found.add((r.claim_id, "P05"))
        if (r.submitted_date - d).days > rule("submit_within_days", d):
            found.add((r.claim_id, "P09"))
        if r.alcohol and not r.client_present:
            found.add((r.claim_id, "P10"))
    meals = lines[lines.category == "Meals"].groupby(["employee_id", "expense_date"]).agg(
        total=("amount", "sum"), claim_id=("claim_id", "max"))
    found |= {(c, "P03") for (e, d), t, c in zip(meals.index, meals.total, meals.claim_id) if t > rule("meals_day", d)}
    dup = lines[lines.duplicated(["employee_id", "expense_date", "merchant", "amount"], keep="first")]
    found |= {(c, "P06") for c in dup.claim_id}
    level = lambda t: 1 if t < 500 else 2 if t <= 2500 else 3
    role_level = {"Manager": 1, "Department head": 2, "Finance director": 3}
    approver_level = {}
    for _, c in claims.iterrows():
        e = emp.loc[c.employee_id]
        chain = {e.line_manager_id, e.dept_head_id, e.finance_director_id}
        lvl = 0 if c.approved_by == c.employee_id or c.approved_by not in chain else role_level[emp.loc[c.approved_by].role]
        approver_level[c.claim_id] = lvl
        if lvl < level(c.claim_total):
            found.add((c.claim_id, "P08"))
    groups = lines.groupby(["employee_id", "merchant", "expense_date"])
    for _, g in groups:
        if g.claim_id.nunique() > 1 and not g.duplicated(["amount"]).any():
            if level(g.amount.sum()) > max(approver_level[c] for c in g.claim_id):
                found.add((g.claim_id.max(), "P07"))

    cur.execute("SELECT claim_id, check_code FROM pol.check_results")
    assert {tuple(r) for r in cur.fetchall()} == found
