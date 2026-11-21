"""
Monthly chargeback process for Halden Group (synthetic data), run as a simulated timeline.

For each reporting month the pipeline replays what a finance team would do:
  - Cut-off run   (working day 3, 18:00): use the files received so far
  - Reruns        (1 hour after any later file arrives, before close)
  - Close         (working day 6, 17:00): controls C01-C08, month-close approval
  - Adjustments   (files that arrive after the month has closed: catch-ups and corrections,
                   posted in the next open period)

Each run: select the latest version of each file -> apply hierarchy changes -> validate ->
post source expenses -> allocate -> journal (draft -> validated -> approved -> posted) ->
update exceptions and escalations -> reconcile.

Usage:
    python src/pipeline.py run-year                          # full process
    python src/pipeline.py run-year --stop-after validate    # checks and exceptions only
    python src/pipeline.py run-year --stop-after allocate    # + allocations, no journals
    python src/pipeline.py rerun --period 2025-03            # re-run a finished month (idempotency check)
    python src/pipeline.py show --period 2025-02             # summary of one month
"""
import argparse
from datetime import date, datetime, time, timedelta

from db import add_connection_args, connect

POOLS = ["P1", "P2", "P3", "P4"]
REVIEWER = "Finance reviewer (simulated)"


# ------------------------------------------------------------------ calendar
class Calendar:
    def __init__(self, cur):
        cur.execute("SELECT holiday_date FROM ref.holidays")
        self.holidays = {r[0] for r in cur.fetchall()}

    def is_working(self, d):
        return d.weekday() < 5 and d not in self.holidays

    def working_day(self, year, month, n):
        d, count = date(year, month, 1), 0
        while True:
            if self.is_working(d):
                count += 1
                if count == n:
                    return d
            d += timedelta(days=1)

    def add_working_days(self, start: datetime, n: int) -> datetime:
        d = start.date()
        while n > 0:
            d += timedelta(days=1)
            if self.is_working(d):
                n -= 1
        return datetime.combine(d, time(17, 0))


def next_month(period):
    y, m = map(int, period.split("-"))
    return (y + 1, 1) if m == 12 else (y, m + 1)


# ------------------------------------------------------------------ timeline
def build_timeline(cur, cal):
    cur.execute("SELECT period FROM ref.periods ORDER BY period")
    periods = [r[0] for r in cur.fetchall()]
    events = []
    for p in periods:
        ny, nm = next_month(p)
        cutoff = datetime.combine(cal.working_day(ny, nm, 3), time(18, 0))
        close = datetime.combine(cal.working_day(ny, nm, 6), time(17, 0))
        events.append((cutoff, 1, "run", p, "Cut-off"))
        events.append((close, 3, "close", p, "Close"))
        cur.execute("SELECT received_at FROM src.files WHERE period = %s AND received_at > %s", (p, cutoff))
        for (received,) in cur.fetchall():
            at = received + timedelta(hours=1)
            if at < close:
                events.append((at, 2, "run", p, "Rerun"))
            else:
                events.append((at, 2, "run", p, "Adjustment"))
    # daily escalation check at 09:00 on every working day, between the first and last event
    first, last = min(e[0] for e in events).date(), max(e[0] for e in events).date()
    d = first
    while d <= last:
        if cal.is_working(d):
            events.append((datetime.combine(d, time(9, 0)), 0, "sweep", "", "Daily check"))
        d += timedelta(days=1)
    return sorted(set(events))


# ------------------------------------------------------------------ helpers
def one(cur, sql, params=()):
    cur.execute(sql, params)
    r = cur.fetchone()
    return r[0] if r else None


def select_files(cur, run_id, period, at):
    """Latest version of each file received by the run time."""
    cur.execute("""
        INSERT INTO ops.run_files
        SELECT %s, file_id FROM (
            SELECT DISTINCT ON (file_type, COALESCE(pool_code, ''))
                   file_id, received_at
            FROM src.files
            WHERE period = %s AND received_at <= %s
            ORDER BY file_type, COALESCE(pool_code, ''), version DESC, received_at DESC) f
        RETURNING file_id""", (run_id, period, at))
    cur.execute("""
        SELECT f.file_type, f.pool_code, f.file_id, f.file_name
        FROM ops.run_files rf JOIN src.files f USING (file_id) WHERE rf.run_id = %s""", (run_id,))
    files = {}
    for ftype, pool, fid, name in cur.fetchall():
        files[(ftype, pool)] = (fid, name)
    return files


def open_posting_period(cur, period, run_type):
    if run_type != "Adjustment":
        return period
    return one(cur, "SELECT MIN(period) FROM ref.periods WHERE status = 'Open' AND period > %s", (period,))


# ------------------------------------------------------------------ journals
def journal_lifecycle(cur, jid, at):
    """Validate -> approve (simulated) -> post. Rejects with a reason if any control fails."""
    reasons = []
    debit, credit = (one(cur, "SELECT COALESCE(SUM(debit),0) FROM fin.journal_lines WHERE journal_id=%s", (jid,)),
                     one(cur, "SELECT COALESCE(SUM(credit),0) FROM fin.journal_lines WHERE journal_id=%s", (jid,)))
    if debit != credit:
        reasons.append(f"not balanced ({debit} vs {credit})")
    if one(cur, """SELECT COUNT(*) FROM fin.journal_lines l LEFT JOIN ref.gl_accounts g USING (account)
                   WHERE l.journal_id = %s AND g.account IS NULL""", (jid,)):
        reasons.append("invalid account")
    if one(cur, """SELECT COUNT(*) FROM fin.journal_lines l JOIN fin.journals j USING (journal_id)
                   JOIN ref.periods p ON p.period = j.source_period
                   WHERE l.journal_id = %s
                     AND NOT EXISTS (SELECT 1 FROM ref.mapping_at(p.period_start) m
                                     WHERE m.cc_code = l.cc_code AND m.is_active)""", (jid,)):
        reasons.append("cost centre not active in the source period")
    if one(cur, """SELECT p.status FROM fin.journals j JOIN ref.periods p ON p.period = j.posting_period
                   WHERE j.journal_id = %s""", (jid,)) != "Open":
        reasons.append("posting period is closed")
    if reasons:
        cur.execute("UPDATE fin.journals SET status='Rejected', rejection_reason=%s WHERE journal_id=%s",
                    ("; ".join(reasons), jid))
        return False
    cur.execute("""UPDATE fin.journals SET status='Posted', validated_at=%s, approved_at=%s, approved_by=%s,
                   posted_at=%s WHERE journal_id=%s""", (at, at, REVIEWER, at, jid))
    cur.execute("""INSERT INTO fin.ledger (journal_id, line_no, posting_period, source_period, cc_code, account,
                                           debit, credit, posted_at)
                   SELECT l.journal_id, l.line_no, j.posting_period, j.source_period, l.cc_code, l.account,
                          l.debit, l.credit, %s
                   FROM fin.journal_lines l JOIN fin.journals j USING (journal_id) WHERE l.journal_id = %s""",
                (at, jid))
    return True


def new_journal(cur, jid, jtype, source_period, posting_period, pool, allocation_id, key, run_id, at, reverses=None):
    cur.execute("""INSERT INTO fin.journals (journal_id, journal_type, source_period, posting_period, pool_code,
                       allocation_id, reverses_journal, status, idempotency_key, created_run_id, created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'Draft',%s,%s,%s)""",
                (jid, jtype, source_period, posting_period, pool, allocation_id, reverses, key, run_id, at))


def post_source_journal(cur, run_id, period, posting_period, pool, expense_file, at):
    key = f"SRC|{period}|{pool}"
    if one(cur, "SELECT 1 FROM fin.journals WHERE idempotency_key=%s AND status='Posted'", (key,)):
        return
    jid = f"SRC-{period}-{pool}"
    new_journal(cur, jid, "Source", period, posting_period, pool, None, key, run_id, at)
    cur.execute("""INSERT INTO fin.journal_lines
                   SELECT %s, line_no, pool_cc, account, amount, 0, source_ref
                   FROM src.expense_lines WHERE file_id = %s""", (jid, expense_file))
    cur.execute("""INSERT INTO fin.journal_lines
                   SELECT %s, 1000, pool_cc, '2100', 0, SUM(amount), 'Accounts payable'
                   FROM src.expense_lines WHERE file_id = %s GROUP BY pool_cc""", (jid, expense_file))
    journal_lifecycle(cur, jid, at)


def post_allocation_journal(cur, run_id, period, posting_period, pool, allocation_id, version, at):
    jtype = "Allocation" if version == 1 and posting_period == period else (
        "Catch-up" if version == 1 else "Replacement")
    jid = f"JE-{period}-{pool}-V{version}"
    new_journal(cur, jid, jtype, period, posting_period, pool, allocation_id,
                f"ALLOC|{period}|{pool}|V{version}", run_id, at)
    cur.execute("""INSERT INTO fin.journal_lines
                   SELECT %s, ROW_NUMBER() OVER (ORDER BY al.cc_code), al.cc_code, cp.allocated_account,
                          al.allocated_amount, 0, %s
                   FROM fin.allocation_lines al JOIN fin.allocations a USING (allocation_id)
                   JOIN ref.cost_pools cp ON cp.pool_code = a.pool_code
                   WHERE al.allocation_id = %s""", (jid, f"{pool} chargeback {period}", allocation_id))
    cur.execute("""INSERT INTO fin.journal_lines
                   SELECT %s, 1000, cp.pool_cc, cp.recovery_account, 0, a.pool_cost, %s
                   FROM fin.allocations a JOIN ref.cost_pools cp USING (pool_code)
                   WHERE a.allocation_id = %s""", (jid, f"{pool} recovery {period}", allocation_id))
    return jid, journal_lifecycle(cur, jid, at)


def reverse_journal(cur, run_id, original, posting_period, at):
    jid = f"REV-{original}"
    period, pool = one(cur, "SELECT source_period FROM fin.journals WHERE journal_id=%s", (original,)), \
        one(cur, "SELECT pool_code FROM fin.journals WHERE journal_id=%s", (original,))
    new_journal(cur, jid, "Reversal", period, posting_period, pool, None, f"REV|{original}", run_id, at, original)
    cur.execute("""INSERT INTO fin.journal_lines
                   SELECT %s, line_no, cc_code, account, credit, debit, 'Reversal of ' || %s
                   FROM fin.journal_lines WHERE journal_id = %s""", (jid, original, original))
    return journal_lifecycle(cur, jid, at)


# ------------------------------------------------------------------ exceptions
def update_exceptions(cur, cal, run_id, period, at):
    cur.execute("""SELECT check_code, pool_code, cc_code, MIN(detail), MAX(amount_at_risk)
                   FROM ops.check_results WHERE run_id = %s
                   GROUP BY check_code, pool_code, cc_code""", (run_id,))
    found = cur.fetchall()
    seen = set()
    for check, pool, cc, detail, amount in found:
        seen.add((check, pool or "", cc or ""))
        ex = one(cur, """SELECT exception_id FROM ops.exceptions
                         WHERE period=%s AND check_code=%s AND COALESCE(pool_code,'')=%s AND COALESCE(cc_code,'')=%s
                           AND status IN ('Open','Reopened')""", (period, check, pool or "", cc or ""))
        if ex:
            cur.execute("UPDATE ops.exceptions SET last_seen_run_id=%s WHERE exception_id=%s", (run_id, ex))
            cur.execute("INSERT INTO ops.exception_events VALUES (%s,%s,%s,'Still failing',%s)", (ex, at, run_id, detail))
            continue
        resolved = one(cur, """SELECT exception_id FROM ops.exceptions
                               WHERE period=%s AND check_code=%s AND COALESCE(pool_code,'')=%s
                                 AND COALESCE(cc_code,'')=%s AND status='Resolved'""",
                       (period, check, pool or "", cc or ""))
        if resolved:
            cur.execute("""UPDATE ops.exceptions SET status='Reopened', resolved_at=NULL, last_seen_run_id=%s
                           WHERE exception_id=%s""", (run_id, resolved))
            cur.execute("INSERT INTO ops.exception_events VALUES (%s,%s,%s,'Reopened',%s)", (resolved, at, run_id, detail))
            continue
        cur.execute("""SELECT c.severity, c.owner_role, c.root_cause, s.working_days, s.escalate_value
                       FROM ops.check_catalogue c JOIN ops.sla s USING (severity) WHERE c.check_code=%s""", (check,))
        severity, owner, root, days, threshold = cur.fetchone()
        due = cal.add_working_days(at, days)
        cur.execute("""INSERT INTO ops.exceptions (period, check_code, pool_code, cc_code, severity, owner_role,
                           root_cause, description, amount_at_risk, status, opened_run_id, opened_at, due_at,
                           last_seen_run_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'Open',%s,%s,%s,%s) RETURNING exception_id""",
                    (period, check, pool, cc, severity, owner, root, detail, amount, run_id, at, due, run_id))
        ex = cur.fetchone()[0]
        cur.execute("INSERT INTO ops.exception_events VALUES (%s,%s,%s,'Opened',%s)", (ex, at, run_id, detail))
        if threshold is not None and amount is not None and amount > threshold:
            cur.execute("UPDATE ops.exceptions SET escalated=TRUE WHERE exception_id=%s", (ex,))
            cur.execute("INSERT INTO ops.exception_events VALUES (%s,%s,%s,'Escalated',%s)",
                        (ex, at, run_id, f"{severity} with amount at risk {amount} above {threshold}"))

    # anything open for this period that no longer fails is resolved
    cur.execute("""SELECT exception_id, check_code, COALESCE(pool_code,''), COALESCE(cc_code,'')
                   FROM ops.exceptions WHERE period=%s AND status IN ('Open','Reopened')""", (period,))
    for ex, check, pool, cc in cur.fetchall():
        if (check, pool, cc) not in seen:
            cur.execute("""UPDATE ops.exceptions SET status='Resolved', resolved_at=%s,
                           resolution_note=%s WHERE exception_id=%s""",
                        (at, f"Check passed in run {run_id}", ex))
            cur.execute("INSERT INTO ops.exception_events VALUES (%s,%s,%s,'Resolved',%s)",
                        (ex, at, run_id, f"Check passed in run {run_id}"))


def escalation_sweep(cur, at):
    cur.execute("""SELECT exception_id, due_at FROM ops.exceptions
                   WHERE status IN ('Open','Reopened') AND NOT escalated AND due_at < %s""", (at,))
    overdue = cur.fetchall()
    for ex, due in overdue:
        cur.execute("UPDATE ops.exceptions SET escalated=TRUE WHERE exception_id=%s", (ex,))
        cur.execute("INSERT INTO ops.exception_events VALUES (%s,%s,NULL,'Escalated',%s)",
                    (ex, at, f"Past due since {due}"))
    return [ex for ex, _ in overdue]


# ------------------------------------------------------------------ one run
def run_month(cur, cal, period, run_type, at, stop_after=None, note=None):
    escalation_sweep(cur, at)  # anything already past due is escalated before this run resolves it
    posting_period = open_posting_period(cur, period, run_type)
    cur.execute("""INSERT INTO ops.runs (period, run_type, run_at, posting_period, note)
                   VALUES (%s,%s,%s,%s,%s) RETURNING run_id""", (period, run_type, at, posting_period, note))
    run_id = cur.fetchone()[0]
    files = select_files(cur, run_id, period, at)

    # hierarchy changes first: an invalid file blocks every pool
    global_block = False
    if ("HIERARCHY", None) in files:
        fid, _ = files[("HIERARCHY", None)]
        cur.execute("SELECT ops.apply_hierarchy_file(%s, %s)", (run_id, fid))
        global_block = bool(one(cur, "SELECT 1 FROM ops.check_results WHERE run_id=%s AND check_code='V07'", (run_id,)))
        if global_block:
            total = one(cur, """SELECT SUM(l.amount) FROM src.expense_lines l
                               JOIN ops.run_files rf USING (file_id) WHERE rf.run_id=%s""", (run_id,))
            cur.execute("UPDATE ops.check_results SET amount_at_risk=%s WHERE run_id=%s AND check_code='V07'",
                        (total, run_id))

    for pool in POOLS:
        exp, vol = files.get(("EXPENSE", pool)), files.get(("VOLUME", pool))
        if not exp or not vol:
            missing = ", ".join(n for n, f in (("expense file", exp), ("volume file", vol)) if not f)
            cur.execute("INSERT INTO ops.check_results VALUES (%s,'V08',%s,NULL,%s,NULL,FALSE)",
                        (run_id, pool, f"{pool}: {missing} not received by the cut-off"))
            cur.execute("INSERT INTO ops.run_pools VALUES (%s,%s,'Waiting for file',NULL)", (run_id, pool))
            continue
        ok = one(cur, "SELECT ops.validate_pool(%s,%s,%s,%s,%s)", (run_id, period, pool, exp[0], vol[0]))
        expense_ok = not one(cur, """SELECT 1 FROM ops.check_results WHERE run_id=%s AND pool_code=%s
                                     AND check_code IN ('V01','V10')""", (run_id, pool))
        cost = one(cur, "SELECT SUM(amount) FROM src.expense_lines WHERE file_id=%s", (exp[0],)) if expense_ok else None

        if stop_after != "validate" and expense_ok:
            post_source_journal(cur, run_id, period, posting_period, pool, exp[0], at)

        if not ok or global_block:
            cur.execute("INSERT INTO ops.run_pools VALUES (%s,%s,'Blocked',%s)", (run_id, pool, cost))
            continue
        if stop_after == "validate":
            cur.execute("INSERT INTO ops.run_pools VALUES (%s,%s,'Validated',%s)", (run_id, pool, cost))
            continue

        # allocate inside a savepoint; drop it again if nothing changed since the last posting
        cur.execute("SAVEPOINT alloc")
        latest = cur.execute("""SELECT a.allocation_id, a.input_hash, a.allocation_version, j.journal_id
                                FROM fin.allocations a
                                LEFT JOIN fin.journals j ON j.allocation_id = a.allocation_id AND j.status = 'Posted'
                                WHERE a.source_period=%s AND a.pool_code=%s
                                  AND (j.journal_id IS NOT NULL OR %s)
                                ORDER BY a.allocation_version DESC, a.allocation_id DESC LIMIT 1""",
                             (period, pool, stop_after == "allocate"))
        latest = cur.fetchone()
        version = (latest[2] + 1) if latest else 1
        alloc_id = one(cur, "SELECT fin.allocate_pool(%s,%s,%s,%s,%s,%s,%s)",
                       (run_id, period, pool, exp[0], vol[0], version, at))
        new_hash = one(cur, "SELECT input_hash FROM fin.allocations WHERE allocation_id=%s", (alloc_id,))
        if latest and latest[1] == new_hash:
            cur.execute("ROLLBACK TO SAVEPOINT alloc")
            cur.execute("INSERT INTO ops.run_pools VALUES (%s,%s,'Already posted',%s)", (run_id, pool, cost))
            continue
        cur.execute("RELEASE SAVEPOINT alloc")
        if stop_after == "allocate":
            cur.execute("INSERT INTO ops.run_pools VALUES (%s,%s,'Allocated',%s)", (run_id, pool, cost))
            continue
        if latest and latest[3]:  # inputs changed after posting: reverse, then replace
            reverse_journal(cur, run_id, latest[3], posting_period, at)
        jid, posted = post_allocation_journal(cur, run_id, period, posting_period, pool, alloc_id, version, at)
        cur.execute("INSERT INTO ops.run_pools VALUES (%s,%s,%s,%s)",
                    (run_id, pool, "Posted" if posted else "Journal rejected", cost))

    update_exceptions(cur, cal, run_id, period, at)
    escalation_sweep(cur, at)
    if stop_after is None:
        cur.execute("SELECT fin.run_reconciliations(%s)", (run_id,))
    return run_id


# ------------------------------------------------------------------ month close
CONTROLS = [
    ("C01", "All source files received"), ("C02", "Hierarchy valid for the period"),
    ("C03", "Volumes complete and valid"), ("C04", "Allocations complete or blocked with an exception"),
    ("C05", "Journals balanced and valid"), ("C06", "No duplicate postings"),
    ("C07", "Ledger agrees with reporting"), ("C08", "Month-close approval"),
]


def close_month(cur, cal, period, at):
    escalation_sweep(cur, at)
    cur.execute("INSERT INTO ops.runs (period, run_type, run_at, posting_period) VALUES (%s,'Close',%s,%s) RETURNING run_id",
                (period, at, period))
    run_id = cur.fetchone()[0]
    select_files(cur, run_id, period, at)
    cur.execute("""INSERT INTO ops.run_pools
                   SELECT %s, pool_code, pool_status, pool_cost FROM (
                       SELECT DISTINCT ON (rp.pool_code) rp.pool_code, rp.pool_status, rp.pool_cost
                       FROM ops.run_pools rp JOIN ops.runs r USING (run_id)
                       WHERE r.period = %s AND r.run_id <> %s
                       ORDER BY rp.pool_code, r.run_at DESC) x""", (run_id, period, run_id))
    cur.execute("SELECT fin.run_reconciliations(%s)", (run_id,))
    open_ex = lambda codes: one(cur, """SELECT COUNT(*) FROM ops.exceptions WHERE period=%s
                                        AND status IN ('Open','Reopened') AND check_code = ANY(%s)""", (period, codes))
    recon_ok = lambda ids: not one(cur, """SELECT COUNT(*) FROM fin.reconciliations
                                         WHERE run_id=%s AND recon_id = ANY(%s) AND result='Fail'""", (run_id, ids))
    files_ok = one(cur, """SELECT COUNT(*) FROM ops.run_files rf JOIN src.files f USING (file_id)
                           WHERE rf.run_id=%s AND f.file_type IN ('EXPENSE','VOLUME')""", (run_id,)) == 8
    unposted = one(cur, """SELECT COUNT(*) FROM fin.journals WHERE posting_period=%s
                           AND status IN ('Draft','Validated','Approved')""", (period,))
    no_alloc_no_ex = one(cur, """SELECT COUNT(*) FROM ref.cost_pools cp
        WHERE NOT EXISTS (SELECT 1 FROM fin.journals j WHERE j.source_period=%s AND j.pool_code=cp.pool_code
                          AND j.journal_type IN ('Allocation','Replacement','Catch-up') AND j.status='Posted')
          AND NOT EXISTS (SELECT 1 FROM ops.exceptions e WHERE e.period=%s AND e.status IN ('Open','Reopened')
                          AND (e.pool_code = cp.pool_code OR e.pool_code IS NULL))""", (period, period))
    critical_open = one(cur, """SELECT COUNT(*) FROM ops.exceptions WHERE period=%s AND severity='Critical'
                                AND status IN ('Open','Reopened')""", (period,))

    results = {
        "C01": ("Pass" if files_ok else "Fail", "src.files manifest / ops.run_files", None),
        "C02": ("Pass" if not open_ex(["V07"]) and recon_ok(["R07"]) else "Fail", "V07 check, R07", None),
        "C03": ("Pass" if not open_ex(["V02", "V03", "V04", "V05", "V06", "V09"]) else "Fail", "Exception report",
                f"{open_ex(['V02', 'V03', 'V04', 'V05', 'V06', 'V09'])} open volume exception(s)"),
        "C04": ("Pass" if no_alloc_no_ex == 0 else "Fail", "R02, allocation schedule", None),
        "C05": ("Pass" if recon_ok(["R04"]) and unposted == 0 else "Fail", "R04, journal report", None),
        "C06": ("Pass" if recon_ok(["R08"]) else "Fail", "R08, run history", None),
        "C07": ("Pass" if recon_ok(["R05", "R06"]) and not one(cur, """SELECT COUNT(*) FROM fin.reconciliations
                 WHERE period=%s AND recon_id IN ('R05','R06') AND result='Fail'""", (period,)) else "Fail",
                "R05, R06", None),
    }
    blocking = [c for c in ("C02", "C05", "C06", "C07") if results[c][0] == "Fail"]
    approved = critical_open == 0 and not blocking
    results["C08"] = ("Approved" if approved else "Rejected", "Close checklist",
                      None if approved else f"{critical_open} critical exception(s) open; failed: {', '.join(blocking) or 'none'}")
    for cid, name in CONTROLS:
        res, evidence, detail = results[cid]
        cur.execute("""INSERT INTO ops.control_results (run_id, period, control_id, control_name, result,
                       evidence_ref, detail, recorded_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (run_id, period, cid, name, res, f"{evidence} (run {run_id})", detail, at))
    if approved:
        cur.execute("UPDATE ref.periods SET status='Closed', closed_at=%s WHERE period=%s", (at, period))
        cur.execute("""INSERT INTO ops.exception_events
                       SELECT exception_id, %s, %s, 'Closed', 'Closed at month close'
                       FROM ops.exceptions WHERE status='Resolved'""", (at, run_id))
        cur.execute("UPDATE ops.exceptions SET status='Closed', closed_at=%s WHERE status='Resolved'", (at,))
    return approved


# ------------------------------------------------------------------ commands
def run_year(args):
    con = connect(args)
    with con, con.cursor() as cur:
        if one(cur, "SELECT COUNT(*) FROM ops.runs"):
            print("The year has already been run. Reload first: python src/load_sources.py")
            return
        cal = Calendar(cur)
        for at, _order, kind, period, run_type in build_timeline(cur, cal):
            if kind == "sweep":
                for ex in escalation_sweep(cur, at):
                    print(f"{at:%Y-%m-%d %H:%M}  daily check: exception #{ex} past due, escalated")
                continue
            if kind == "run":
                rid = run_month(cur, cal, period, run_type, at, args.stop_after)
                cur.execute("SELECT pool_code || ' ' || pool_status FROM ops.run_pools WHERE run_id=%s ORDER BY 1", (rid,))
                print(f"{at:%Y-%m-%d %H:%M}  {period}  {run_type:<10}  run {rid:<3} " +
                      " | ".join(r[0] for r in cur.fetchall()))
            elif args.stop_after is None:
                ok = close_month(cur, cal, period, at)
                print(f"{at:%Y-%m-%d %H:%M}  {period}  Close       {'approved' if ok else 'REJECTED'}")
        summary(cur)
    con.close()


def summary(cur):
    q = lambda sql: one(cur, sql)
    print("\nSummary")
    print(f"  runs                {q('SELECT COUNT(*) FROM ops.runs')}")
    print(f"  exceptions          {q('SELECT COUNT(*) FROM ops.exceptions')} "
          f"(open {q(chr(39).join(['SELECT COUNT(*) FROM ops.exceptions WHERE status IN (', 'Open', ',', 'Reopened', ')']))}, "
          f"escalated {q('SELECT COUNT(*) FROM ops.exceptions WHERE escalated')})")
    print(f"  journals posted     {q(chr(39).join(['SELECT COUNT(*) FROM fin.journals WHERE status=', 'Posted', '']))}")
    print(f"  allocated (net)     {q(chr(39).join(['SELECT SUM(l.debit - l.credit) FROM fin.ledger l JOIN ref.gl_accounts g USING (account) WHERE g.account_type = ', 'Allocated expense', '']))}")
    print(f"  shared cost posted  {q(chr(39).join(['SELECT SUM(l.debit - l.credit) FROM fin.ledger l JOIN ref.gl_accounts g USING (account) WHERE g.account_type = ', 'Source expense', '']))}")
    print(f"  failed recons       {q(chr(39).join(['SELECT COUNT(*) FROM fin.reconciliations WHERE result=', 'Fail', '']))}")
    print(f"  periods closed      {q(chr(39).join(['SELECT COUNT(*) FROM ref.periods WHERE status=', 'Closed', '']))}")


def rerun(args):
    con = connect(args)
    with con, con.cursor() as cur:
        cal = Calendar(cur)
        before = one(cur, "SELECT COUNT(*) FROM fin.journals")
        last = one(cur, "SELECT MAX(run_at) FROM ops.runs") or datetime(2026, 1, 31)
        rid = run_month(cur, cal, args.period, "Rerun", last + timedelta(hours=1), note="Manual rerun")
        after = one(cur, "SELECT COUNT(*) FROM fin.journals")
        cur.execute("SELECT pool_code, pool_status FROM ops.run_pools WHERE run_id=%s ORDER BY 1", (rid,))
        for pool, status in cur.fetchall():
            print(f"  {pool}  {status}")
        print(f"Journals before {before}, after {after}: {after - before} new")
    con.close()


def show(args):
    con = connect(args)
    with con, con.cursor() as cur:
        p = args.period
        print(f"== {p}")
        cur.execute("SELECT run_id, run_type, run_at, posting_period FROM ops.runs WHERE period=%s ORDER BY run_at", (p,))
        for r in cur.fetchall():
            cur.execute("SELECT pool_code || ' ' || pool_status FROM ops.run_pools WHERE run_id=%s ORDER BY 1", (r[0],))
            print(f"  run {r[0]:<3} {r[1]:<10} {r[2]:%Y-%m-%d %H:%M} posting {r[3]}: " +
                  " | ".join(x[0] for x in cur.fetchall()))
        cur.execute("""SELECT exception_id, check_code, severity, COALESCE(pool_code,'-'), COALESCE(cc_code,'-'),
                              status, escalated, description FROM ops.exceptions WHERE period=%s ORDER BY 1""", (p,))
        rows = cur.fetchall()
        print(f"  exceptions: {len(rows)}")
        for r in rows:
            print(f"    #{r[0]} {r[1]} {r[2]:<8} {r[3]} {r[4]:<7} {r[5]:<8} {'escalated' if r[6] else '':<9} {r[7]}")
        cur.execute("""SELECT journal_id, journal_type, posting_period, status FROM fin.journals
                       WHERE source_period=%s ORDER BY journal_id""", (p,))
        rows = cur.fetchall()
        print(f"  journals: {len(rows)}")
        for r in rows:
            print(f"    {r[0]:<24} {r[1]:<11} posted in {r[2]}  {r[3]}")
        cur.execute("""SELECT control_id, result, COALESCE(detail,'') FROM ops.control_results
                       WHERE period=%s ORDER BY control_id""", (p,))
        rows = cur.fetchall()
        if rows:
            print("  controls: " + "  ".join(f"{c}:{r}" for c, r, _ in rows))
    con.close()


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("run-year")
    a.add_argument("--stop-after", choices=["validate", "allocate"])
    b = sub.add_parser("rerun")
    b.add_argument("--period", required=True)
    c = sub.add_parser("show")
    c.add_argument("--period", required=True)
    for s in (a, b, c):
        add_connection_args(s)
    args = p.parse_args()
    {"run-year": run_year, "rerun": rerun, "show": show}[args.command](args)


if __name__ == "__main__":
    main()
