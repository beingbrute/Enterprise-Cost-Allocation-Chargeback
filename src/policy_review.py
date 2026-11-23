"""
Monthly expense policy review for Halden Group (synthetic data), run as a simulated timeline.

For each month:
  - Review run (working day 2 of the next month, 18:00): every claim paid in the month is checked
    against the policy version in force on each expense date (pol.run_checks, checks P01-P10).
    Each failed check opens a case: severity, owner, amount in question, decision due date.
    High cases over $1,000 are escalated at intake; an employee's third case in 12 months is
    reported to their business unit finance director.
  - Reviewer actions (simulated from data/raw/policy/reviewer_actions.csv): triage, evidence,
    decision. Disallowed amounts go to remediation (recovery from the employee). A failed recovery
    reopens the case. A case closes only when its decision has evidence and any recovery is confirmed.
  - Daily check at 09:00 on working days: past-due decisions and recoveries are escalated.
  - Governance controls G01-G06 for the month (working day 5 of the next month, 17:00).

The timeline ends on 30 Jan 2026 (last working day reviewed). Anything still open then is reported
as open.

Usage:
    python src/policy_review.py run-year     --port 5432
    python src/policy_review.py show --case 4 --port 5432
"""
import argparse
import heapq
from datetime import date, datetime, time, timedelta

from db import add_connection_args, connect
from pipeline import Calendar, one

END = datetime(2026, 1, 30, 23, 59)


def at(d, h, m=0):
    return datetime.combine(d, time(h, m))


class Review:
    def __init__(self, cur):
        self.cur = cur
        self.cal = Calendar(cur)
        self.queue = []
        self.seq = 0

    # ------------------------------------------------------------------ helpers
    def push(self, when, kind, **payload):
        self.seq += 1
        heapq.heappush(self.queue, (when, self.seq, kind, payload))

    def wd(self, start, n, hour, minute=0):
        """n working days after the date of `start`, at the given time (n = 0: same day)."""
        d = start.date() if isinstance(start, datetime) else start
        if n == 0:
            return at(d, hour, minute)
        return at(self.cal.add_working_days(at(d, 0), n).date(), hour, minute)

    def event(self, case_id, when, kind, detail):
        self.cur.execute("INSERT INTO pol.case_events VALUES (%s,%s,%s,%s)", (case_id, when, kind, detail))

    def log(self, when, text):
        print(f"{when:%Y-%m-%d %H:%M}  {text}")

    # ------------------------------------------------------------------ timeline
    def build(self):
        for m in range(1, 13):
            period = f"2025-{m:02d}"
            ny, nm = (2026, 1) if m == 12 else (2025, m + 1)
            self.push(at(self.cal.working_day(ny, nm, 2), 18), "review", period=period)
            self.push(at(self.cal.working_day(ny, nm, 5), 17), "governance", period=period)
        d = date(2025, 2, 1)
        while d <= END.date():
            if self.cal.is_working(d):
                self.push(at(d, 9), "sweep")
            d += timedelta(days=1)

    def run(self):
        self.build()
        while self.queue:
            when, _, kind, p = heapq.heappop(self.queue)
            if when > END:
                break
            getattr(self, "on_" + kind)(when, **p)

    # ------------------------------------------------------------------ events
    def on_review(self, when, period):
        cur = self.cur
        cur.execute("INSERT INTO pol.review_runs (period, run_at) VALUES (%s,%s) RETURNING run_id", (period, when))
        run_id = cur.fetchone()[0]
        cur.execute("SELECT pol.run_checks(%s, %s)", (run_id, period))
        found = cur.fetchone()[0]
        checked = one(cur, "SELECT claims_checked FROM pol.review_runs WHERE run_id=%s", (run_id,))
        self.log(when, f"{period}  review run {run_id}: {checked} claims checked, {found} case(s)")
        cur.execute("""SELECT r.claim_id, r.check_code, r.amount_in_question, r.detail, c.employee_id, k.severity,
                              s.decision_wd, s.escalate_at_intake
                       FROM pol.check_results r JOIN pol.claims c USING (claim_id)
                       JOIN pol.check_catalogue k USING (check_code) JOIN pol.sla s USING (severity)
                       WHERE r.run_id = %s ORDER BY r.claim_id, r.check_code""", (run_id,))
        for claim, check, amount, detail, emp, sev, days, esc_over in cur.fetchall():
            due = self.wd(when, days, 17)
            cur.execute("""INSERT INTO pol.cases (claim_id, check_code, employee_id, period, severity, amount_in_question,
                               detail, status, opened_run_id, opened_at, decision_due_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,'Open',%s,%s,%s) RETURNING case_id""",
                        (claim, check, emp, period, sev, amount, detail, run_id, when, due))
            case_id = cur.fetchone()[0]
            self.event(case_id, when, "Opened", detail)
            self.log(when, f"    case #{case_id} {check} {sev:<6} {claim} {emp}  {detail}")
            if esc_over is not None and amount > esc_over:
                cur.execute("UPDATE pol.cases SET escalated=TRUE WHERE case_id=%s", (case_id,))
                self.event(case_id, when, "Escalated", f"{sev} with {amount} in question, above {esc_over}")
                self.log(when, f"    case #{case_id} escalated at intake")
            self.repeat_check(case_id, emp, when)
            cur.execute("SELECT * FROM pol.reviewer_actions WHERE claim_id=%s AND check_code=%s", (claim, check))
            a = cur.fetchone()
            if a is None:
                self.log(when, f"    case #{case_id}: no reviewer action on file; stays open")
                continue
            (_, _, decision, disallowed, triage_wd, decide_wd, evidence,
             recover_wd, recovery_result, retry_wd, note) = a
            self.push(self.wd(when, 1 + triage_wd, 10), "triage", case_id=case_id)
            self.push(self.wd(when, decide_wd, 15), "decide", case_id=case_id, decision=decision,
                      disallowed=disallowed, evidence=evidence, note=note,
                      recover_wd=recover_wd, result=recovery_result, retry_wd=retry_wd)

    def repeat_check(self, case_id, emp, when):
        cur = self.cur
        n = one(cur, """SELECT COUNT(*) FROM pol.cases WHERE employee_id=%s
                        AND opened_at > %s - INTERVAL '365 days'""", (emp, when))
        if n >= 3:
            fd = one(cur, "SELECT finance_director_id FROM pol.employees WHERE employee_id=%s", (emp,))
            cur.execute("INSERT INTO pol.repeat_reports VALUES (%s,%s,%s,%s)", (emp, when, n, fd))
            self.event(case_id, when, "Repeat reported", f"{n} cases in 12 months; reported to {fd}")
            self.log(when, f"    {emp}: {n} cases in 12 months, reported to finance director {fd}")

    def on_triage(self, when, case_id):
        self.cur.execute("UPDATE pol.cases SET status='Triaged', triaged_at=%s WHERE case_id=%s AND status='Open'",
                         (when, case_id))
        self.event(case_id, when, "Triaged", "Assigned to Expense Policy Analyst")

    def on_decide(self, when, case_id, decision, disallowed, evidence, note, recover_wd, result, retry_wd):
        cur = self.cur
        cur.execute("""UPDATE pol.cases SET decision=%s, decided_at=%s, disallowed=%s, evidence=%s, status='Decided'
                       WHERE case_id=%s RETURNING decision_due_at, employee_id, claim_id, check_code""",
                    (decision, when, disallowed, evidence, case_id))
        due, emp, claim, check = cur.fetchone()
        late = " (after the due date)" if when > due else ""
        self.event(case_id, when, "Decided", f"{decision}; disallowed {disallowed}; evidence: {evidence}"
                   + (f"; {note}" if note else ""))
        self.log(when, f"    case #{case_id} decided: {decision}, disallowed {disallowed}{late}")
        if decision == "Exception approved":
            n = one(cur, "SELECT COUNT(*) FROM pol.exception_register") + 1
            approver = one(cur, "SELECT dept_head_id FROM pol.employees WHERE employee_id=%s", (emp,))
            cur.execute("""INSERT INTO pol.exception_register (exception_id, employee_id, rule_code, reason, approved_by,
                               source, case_id, recorded_at) VALUES (%s,%s,%s,%s,%s,'Review decision',%s,%s)""",
                        (f"X{n:03d}", emp, check, note or f"Approved in case {case_id}", approver, case_id, when))
            self.event(case_id, when, "Exception recorded", f"X{n:03d} for {claim}")
        if disallowed > 0:
            cur.execute("UPDATE pol.cases SET status='Remediation', remediation_due_at=%s WHERE case_id=%s",
                        (self.wd(when, 20, 17), case_id))
            self.event(case_id, when, "Recovery requested", f"{disallowed} from {emp}")
            if recover_wd is not None:
                self.push(self.wd(when, recover_wd, 12), "recover", case_id=case_id, result=result, retry_wd=retry_wd)
        else:
            self.close(case_id, when)

    def on_recover(self, when, case_id, result, retry_wd):
        cur = self.cur
        if result == "Failed":
            cur.execute("UPDATE pol.cases SET status='Reopened' WHERE case_id=%s", (case_id,))
            self.event(case_id, when, "Recovery failed", "Payroll deduction returned; case reopened")
            self.log(when, f"    case #{case_id} recovery failed, reopened")
            self.push(self.wd(when, retry_wd, 12), "recover", case_id=case_id, result="Recovered", retry_wd=None)
            return
        cur.execute("UPDATE pol.cases SET recovered=disallowed WHERE case_id=%s RETURNING disallowed", (case_id,))
        amount = cur.fetchone()[0]
        self.event(case_id, when, "Recovered", f"{amount} recovered")
        self.log(when, f"    case #{case_id} {amount} recovered")
        self.close(case_id, when)

    def close(self, case_id, when):
        """A case closes only when its decision has evidence and any recovery is complete."""
        cur = self.cur
        cur.execute("SELECT evidence, disallowed, recovered FROM pol.cases WHERE case_id=%s", (case_id,))
        evidence, disallowed, recovered = cur.fetchone()
        if not evidence or recovered < disallowed:
            self.event(case_id, when, "Close refused", "Evidence or recovery missing")
            return
        cur.execute("UPDATE pol.cases SET status='Closed', closed_at=%s WHERE case_id=%s", (when, case_id))
        self.event(case_id, when, "Closed", None)
        self.log(when, f"    case #{case_id} closed")

    def on_sweep(self, when):
        cur = self.cur
        cur.execute("""SELECT case_id, 'decision', decision_due_at FROM pol.cases
                       WHERE NOT escalated AND decided_at IS NULL AND decision_due_at < %s
                       UNION ALL
                       SELECT case_id, 'recovery', remediation_due_at FROM pol.cases
                       WHERE NOT escalated AND status IN ('Remediation', 'Reopened') AND remediation_due_at < %s""",
                    (when, when))
        for case_id, what, due in cur.fetchall():
            cur.execute("UPDATE pol.cases SET escalated=TRUE WHERE case_id=%s", (case_id,))
            self.event(case_id, when, "Escalated", f"{what.capitalize()} past due since {due:%Y-%m-%d %H:%M}")
            self.log(when, f"daily check: case #{case_id} {what} past due, escalated to the Manager")

    def on_governance(self, when, period):
        cur = self.cur
        cur.execute("SELECT run_id, claims_checked, run_at FROM pol.review_runs WHERE period=%s", (period,))
        run_id, checked, run_at = cur.fetchone()
        paid = one(cur, "SELECT COUNT(*) FROM pol.claims WHERE to_char(paid_date, 'YYYY-MM')=%s", (period,))
        late_triage = one(cur, """SELECT COUNT(*) FROM pol.cases WHERE period=%s
                                  AND (triaged_at IS NULL OR triaged_at > %s)""",
                          (period, self.wd(run_at, 1, 23, 59)))
        closed_no_evidence = one(cur, """SELECT COUNT(*) FROM pol.cases WHERE status='Closed'
                                         AND (evidence IS NULL OR recovered < disallowed)""")
        past_due_not_escalated = one(cur, """SELECT COUNT(*) FROM pol.cases WHERE NOT escalated AND (
                                       (decided_at IS NULL AND decision_due_at < %s) OR
                                       (status IN ('Remediation','Reopened') AND remediation_due_at < %s))""",
                                     (when, when))
        cur.execute("""SELECT COALESCE(SUM(disallowed),0), COALESCE(SUM(recovered),0),
                              COALESCE(SUM(disallowed - recovered) FILTER (WHERE status IN ('Remediation','Reopened')),0)
                       FROM pol.cases WHERE decided_at IS NOT NULL""")
        dis, rec, outstanding = cur.fetchone()
        results = [
            ("G01", "Every claim paid in the month was checked", checked == paid,
             f"pol.review_runs run {run_id}", f"{checked} of {paid} claims checked"),
            ("G02", "Every case opened was triaged within 1 working day", late_triage == 0,
             "pol.cases triaged_at", f"{late_triage} case(s) triaged late or not at all"),
            ("G03", "No case closed without evidence and full recovery", closed_no_evidence == 0,
             "pol.cases evidence, recovered", f"{closed_no_evidence} case(s)"),
            ("G04", "Every past-due case was escalated", past_due_not_escalated == 0,
             "pol.case_events (Escalated)", f"{past_due_not_escalated} past-due case(s) not escalated"),
            ("G05", "Disallowed = recovered + outstanding", dis == rec + outstanding,
             "pol.cases disallowed, recovered", f"disallowed {dis}, recovered {rec}, outstanding {outstanding}"),
            ("G06", "Governance report issued for the month", True,
             f"rpt.policy_* views as at {when:%Y-%m-%d %H:%M}", None),
        ]
        for cid, name, ok, evidence, detail in results:
            cur.execute("INSERT INTO pol.governance_results (period, control_id, control_name, result, evidence_ref, "
                        "detail, recorded_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (period, cid, name, "Pass" if ok else "Fail", evidence, detail, when))
        fails = [c for c, _, ok, _, _ in results if not ok]
        self.log(when, f"{period}  governance controls: {6 - len(fails)} of 6 passed" + (f" (failed: {', '.join(fails)})" if fails else ""))


def run_year(args):
    con = connect(args)
    with con, con.cursor() as cur:
        if one(cur, "SELECT COUNT(*) FROM pol.review_runs"):
            print("The policy year has already been run. Reload first: python src/load_policy.py")
            return
        Review(cur).run()
        summary(cur)
    con.close()


def summary(cur):
    q = lambda sql: one(cur, sql)
    print("\nSummary")
    print(f"  claims checked      {q('SELECT SUM(claims_checked) FROM pol.review_runs')} of {q('SELECT COUNT(*) FROM pol.claims')}")
    print(f"  cases               {q('SELECT COUNT(*) FROM pol.cases')} "
          f"(escalated {q('SELECT COUNT(*) FROM pol.cases WHERE escalated')}, "
          f"open {q(chr(39).join(['SELECT COUNT(*) FROM pol.cases WHERE status <> ', 'Closed', '']))})")
    cur.execute("SELECT decision, COUNT(*) FROM pol.cases GROUP BY 1 ORDER BY 1")
    print("  decisions           " + ", ".join(f"{d} {n}" for d, n in cur.fetchall()))
    cur.execute("SELECT SUM(disallowed), SUM(recovered) FROM pol.cases")
    dis, rec = cur.fetchone()
    print(f"  disallowed          {dis}  (recovered {rec}, outstanding {dis - rec})")
    print(f"  repeat reports      {q('SELECT COUNT(*) FROM pol.repeat_reports')}")
    print(f"  exceptions added    {q(chr(39).join(['SELECT COUNT(*) FROM pol.exception_register WHERE source = ', 'Review decision', '']))}")
    print(f"  governance controls {q(chr(39).join(['SELECT COUNT(*) FROM pol.governance_results WHERE result = ', 'Pass', '']))} "
          f"of {q('SELECT COUNT(*) FROM pol.governance_results')} passed")


def show(args):
    con = connect(args)
    with con, con.cursor() as cur:
        cur.execute("""SELECT case_id, claim_id, check_code, severity, employee_id, status, decision, disallowed,
                              recovered, escalated, detail FROM pol.cases WHERE case_id=%s""", (args.case,))
        r = cur.fetchone()
        if not r:
            print("No such case")
            return
        print(f"Case #{r[0]}  {r[2]} {r[3]}  claim {r[1]}  employee {r[4]}")
        print(f"  {r[10]}")
        print(f"  status {r[5]}, decision {r[6]}, disallowed {r[7]}, recovered {r[8]}, escalated {r[9]}")
        cur.execute("SELECT event_at, event_type, COALESCE(detail,'') FROM pol.case_events WHERE case_id=%s ORDER BY event_at",
                    (args.case,))
        for when, kind, detail in cur.fetchall():
            print(f"  {when:%Y-%m-%d %H:%M}  {kind:<18} {detail}")
    con.close()


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("run-year")
    b = sub.add_parser("show")
    b.add_argument("--case", type=int, required=True)
    for s in (a, b):
        add_connection_args(s)
    args = p.parse_args()
    {"run-year": run_year, "show": show}[args.command](args)


if __name__ == "__main__":
    main()
