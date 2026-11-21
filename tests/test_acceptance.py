"""Acceptance tests: the finished year must match the answer key and the business rules in docs/design.md."""
import csv
from datetime import datetime
from decimal import Decimal

import pytest

from conftest import ROOT
from pipeline import Calendar, close_month, run_month

ANSWER_KEY = ROOT / "data" / "answer_key" / "expected_exceptions.csv"


def expected_exceptions():
    """Rows of the answer key that are validation checks (V01-V09), one row per cost centre."""
    rows = set()
    with open(ANSWER_KEY, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r["check_code"].startswith("V"):
                continue
            for cc in (r["cc_code"] or "").split(";"):
                rows.add((r["period"], r["check_code"], r["severity"], cc))
    return rows


# ------------------------------------------------------------------ exceptions
def test_exceptions_match_answer_key(cur):
    cur.execute("SELECT period, check_code, severity, COALESCE(cc_code, '') FROM ops.exceptions")
    assert set(cur.fetchall()) == expected_exceptions()


def test_no_exception_left_open_and_every_month_closed(one):
    assert one("SELECT COUNT(*) FROM ops.exceptions WHERE status <> 'Closed'") == 0
    assert one("SELECT COUNT(*) FROM ref.periods WHERE status = 'Closed'") == 12


def test_escalations(cur, one):
    cur.execute("SELECT period, check_code FROM ops.exceptions WHERE escalated ORDER BY period")
    assert cur.fetchall() == [("2025-02", "V01"), ("2025-06", "V04"), ("2025-11", "V07")]
    # June V04 went past due on 8 Jul 17:00; the daily 09:00 check escalates it the next morning,
    # not when the late file finally arrives on 14 Jul.
    assert one("SELECT escalated_at FROM rpt.exceptions WHERE period='2025-06' AND check_code='V04'") \
        == datetime(2025, 7, 9, 9, 0)


def test_only_june_missed_sla(cur):
    cur.execute("SELECT period, check_code FROM rpt.exceptions WHERE NOT resolved_within_sla")
    assert cur.fetchall() == [("2025-06", "V04")]


# ------------------------------------------------------------------ allocation
def test_everything_posted_is_charged_back(one):
    shared = one("SELECT SUM(pool_cost) FROM rpt.allocation_summary")
    charged = one("SELECT SUM(amount) FROM rpt.fact_chargeback")
    assert shared == charged == Decimal("7625228.08")


def test_each_month_and_pool_fully_allocated(cur):
    cur.execute("""SELECT period, pool_code, pool_cost, allocated_total FROM rpt.allocation_summary
                   WHERE pool_cost <> allocated_total OR unallocated_now <> 0""")
    assert cur.fetchall() == []


def test_business_unit_totals(cur):
    cur.execute("SELECT bu_code, SUM(amount) FROM rpt.fact_chargeback GROUP BY 1 ORDER BY 1")
    assert cur.fetchall() == [("BU01", Decimal("2616996.51")), ("BU02", Decimal("1055171.21")),
                              ("BU03", Decimal("1641673.28")), ("BU04", Decimal("1359097.32")),
                              ("BU05", Decimal("952289.76"))]


def test_june_office_cost_unallocated_at_close_then_caught_up(cur, one):
    cur.execute("SELECT period, pool_code, unallocated_at_close FROM rpt.allocation_summary "
                "WHERE unallocated_at_close <> 0")
    assert cur.fetchall() == [("2025-06", "P4", Decimal("203890.15"))]
    assert one("SELECT posting_period FROM fin.journals WHERE journal_type = 'Catch-up'") == "2025-07"


def test_hierarchy_changes_follow_effective_dates(one):
    # CC1023 moved to department D09 (business unit BU04) from 1 April
    assert one("SELECT DISTINCT bu_code FROM rpt.fact_chargeback WHERE cc_code='CC1023' AND source_period='2025-03'") == "BU02"
    assert one("SELECT DISTINCT bu_code FROM rpt.fact_chargeback WHERE cc_code='CC1023' AND source_period='2025-04'") == "BU04"
    # Department D10 (Customer Service) moved to BU01 from 1 July
    assert one("SELECT DISTINCT bu_code FROM rpt.fact_chargeback WHERE dept_code='D10' AND source_period='2025-07'") == "BU01"


def test_closed_cost_centre_gets_nothing_after_closure(one):
    assert one("SELECT COUNT(*) FROM rpt.fact_chargeback WHERE cc_code='CC1047' AND source_period >= '2025-09'") == 0


def test_policy_change_from_july(one):
    assert one("SELECT driver FROM rpt.allocation_summary WHERE pool_code='P2' AND period='2025-06'") == "tickets_raised"
    assert one("SELECT driver FROM rpt.allocation_summary WHERE pool_code='P2' AND period='2025-07'") == "tickets_resolved"


# ------------------------------------------------------------------ journals
def test_journals_all_posted_and_balanced(one):
    assert one("SELECT COUNT(*) FROM fin.journals WHERE status = 'Posted'") == 98
    assert one("SELECT COUNT(*) FROM fin.journals WHERE status <> 'Posted'") == 0
    assert one("SELECT COUNT(*) FROM rpt.journals WHERE total_debit <> total_credit") == 0


def test_april_correction_is_reversal_plus_replacement(cur, one):
    cur.execute("""SELECT journal_id, journal_type, posting_period, reverses_journal FROM fin.journals
                   WHERE source_period='2025-04' AND pool_code='P3' AND journal_type <> 'Source' ORDER BY journal_id""")
    assert cur.fetchall() == [("JE-2025-04-P3-V1", "Allocation", "2025-04", None),
                              ("JE-2025-04-P3-V2", "Replacement", "2025-06", None),
                              ("REV-JE-2025-04-P3-V1", "Reversal", "2025-06", "JE-2025-04-P3-V1")]
    # CC1015's headcount was corrected from 108 to 88, so its share fell from 3,335.35 to 2,730.43
    assert one("""SELECT SUM(amount) FROM rpt.fact_chargeback
                  WHERE cc_code='CC1015' AND source_period='2025-04' AND pool_code='P3'""") == Decimal("2730.43")


def test_rerunning_a_finished_month_posts_nothing_new(cur, one):
    before = one("SELECT COUNT(*) FROM fin.journals")
    last = one("SELECT MAX(run_at) FROM ops.runs")
    run_month(cur, Calendar(cur), "2025-03", "Rerun", last.replace(year=last.year + 1), note="Test rerun")
    assert one("SELECT COUNT(*) FROM fin.journals") == before


# ------------------------------------------------------------------ reconciliations and controls
def test_only_failed_reconciliation_is_february_cut_off(cur):
    cur.execute("SELECT DISTINCT period, recon_id, pool_code FROM fin.reconciliations WHERE result = 'Fail'")
    assert cur.fetchall() == [("2025-02", "R01", "P1")]


def test_only_failed_control_is_june_c03(cur, one):
    cur.execute("SELECT period, control_id FROM ops.control_results WHERE result NOT IN ('Pass', 'Approved')")
    assert cur.fetchall() == [("2025-06", "C03")]
    assert one("SELECT COUNT(*) FROM ops.control_results WHERE control_id='C08' AND result='Approved'") == 12


@pytest.mark.parametrize("severity, approved", [("High", True), ("Critical", False)])
def test_open_critical_exception_blocks_close(cur, one, severity, approved):
    """An open Critical exception must stop the month-close approval (C08); an open High one must not."""
    run_id = one("SELECT MAX(run_id) FROM ops.runs WHERE period='2025-12'")
    cur.execute("""INSERT INTO ops.exceptions (period, check_code, pool_code, severity, owner_role, root_cause,
                       description, status, opened_run_id, opened_at, due_at, last_seen_run_id)
                   VALUES ('2025-12', 'V01', 'P1', %s, 'Test', 'Test', 'Test exception', 'Open',
                           %s, '2026-01-08 18:00', '2026-01-09 17:00', %s)""", (severity, run_id, run_id))
    result = close_month(cur, Calendar(cur), "2025-12", datetime(2026, 1, 9, 16, 0))
    assert result is approved
    c08 = one("SELECT result FROM ops.control_results WHERE period='2025-12' AND control_id='C08' "
              "ORDER BY run_id DESC LIMIT 1")
    assert c08 == ("Approved" if approved else "Rejected")
