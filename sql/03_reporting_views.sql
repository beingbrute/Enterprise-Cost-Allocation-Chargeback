-- =====================================================================
-- 03  Reporting views (schema rpt) for the Excel workpaper and Power BI
-- Built only from validated, posted data: Excel and Power BI never
-- read the raw files or unposted allocations.
-- Run after src/pipeline.py run-year.
-- =====================================================================

-- ---------------------------------------------------------------- dimensions
CREATE OR REPLACE VIEW rpt.dim_period AS
SELECT period, period_start, period_end,
       EXTRACT(MONTH FROM period_start)::INT AS month_no,
       TO_CHAR(period_start, 'Mon YYYY')     AS period_label,
       status, closed_at
FROM ref.periods;

CREATE OR REPLACE VIEW rpt.dim_pool AS
SELECT cp.pool_code, cp.pool_name, cp.pool_cc, cp.source_account, cp.allocated_account
FROM ref.cost_pools cp;

CREATE OR REPLACE VIEW rpt.dim_business_unit AS
SELECT bu_code, bu_name FROM ref.business_units WHERE bu_code <> 'BU90';

-- Cost centre as at each period (the hierarchy that was true that month)
CREATE OR REPLACE VIEW rpt.dim_cost_centre_period AS
SELECT p.period, m.cc_code, m.cc_name, m.dept_code, d.dept_name, m.bu_code, b.bu_name,
       m.remote_only, m.is_active
FROM ref.periods p
CROSS JOIN LATERAL ref.mapping_at(p.period_start) m
LEFT JOIN ref.departments d ON d.dept_code = m.dept_code AND p.period_start BETWEEN d.valid_from AND d.valid_to
LEFT JOIN ref.business_units b ON b.bu_code = m.bu_code
WHERE m.bu_code <> 'BU90';

-- ---------------------------------------------------------------- chargeback fact
-- One row per posted chargeback line (allocations, catch-ups, replacements and reversals).
-- amount = debit - credit on the allocated-expense accounts, so reversals subtract.
CREATE OR REPLACE VIEW rpt.fact_chargeback AS
SELECT lg.ledger_id, lg.journal_id, j.journal_type, j.pool_code,
       lg.source_period, lg.posting_period,
       lg.cc_code, cc.dept_code, cc.bu_code,
       lg.account, lg.debit - lg.credit AS amount
FROM fin.ledger lg
JOIN fin.journals j USING (journal_id)
JOIN ref.gl_accounts g ON g.account = lg.account AND g.account_type = 'Allocated expense'
JOIN ref.periods p ON p.period = lg.source_period
JOIN LATERAL ref.mapping_at(p.period_start) cc ON cc.cc_code = lg.cc_code;

-- ---------------------------------------------------------------- allocation summary
-- Per month and pool: cost, allocated (net of reversals), unallocated, rate and status at close
CREATE OR REPLACE VIEW rpt.allocation_summary AS
WITH cost AS (
    SELECT j.source_period AS period, j.pool_code, SUM(l.debit - l.credit) AS pool_cost
    FROM fin.ledger l JOIN fin.journals j USING (journal_id)
    JOIN ref.gl_accounts g ON g.account = l.account AND g.account_type = 'Source expense'
    GROUP BY 1, 2
), alloc AS (
    SELECT source_period AS period, pool_code, SUM(amount) AS allocated,
           SUM(amount) FILTER (WHERE posting_period = source_period) AS allocated_in_month
    FROM rpt.fact_chargeback GROUP BY 1, 2
), cur AS (
    SELECT DISTINCT ON (a.source_period, a.pool_code)
           a.source_period AS period, a.pool_code, a.driver, a.total_units, a.rate, a.allocation_version
    FROM fin.allocations a JOIN fin.journals j ON j.allocation_id = a.allocation_id AND j.status = 'Posted'
    ORDER BY a.source_period, a.pool_code, a.allocation_version DESC
), at_close AS (
    SELECT r.period, rp.pool_code, rp.pool_status
    FROM ops.runs r JOIN ops.run_pools rp USING (run_id) WHERE r.run_type = 'Close'
)
SELECT c.period, c.pool_code, cp.pool_name, cur.driver, cur.total_units, cur.rate,
       cur.allocation_version,
       c.pool_cost,
       COALESCE(a.allocated, 0)                         AS allocated_total,
       c.pool_cost - COALESCE(a.allocated, 0)           AS unallocated_now,
       COALESCE(a.allocated_in_month, 0)                AS allocated_by_close,
       c.pool_cost - COALESCE(a.allocated_in_month, 0)  AS unallocated_at_close,
       ac.pool_status                                    AS status_at_close
FROM cost c
JOIN ref.cost_pools cp USING (pool_code)
LEFT JOIN alloc a USING (period, pool_code)
LEFT JOIN cur USING (period, pool_code)
LEFT JOIN at_close ac USING (period, pool_code);

-- ---------------------------------------------------------------- journals
CREATE OR REPLACE VIEW rpt.journals AS
SELECT j.journal_id, j.journal_type, j.source_period, j.posting_period, j.pool_code,
       j.status, j.reverses_journal, j.created_at, j.approved_by, j.posted_at,
       SUM(l.debit) AS total_debit, SUM(l.credit) AS total_credit,
       SUM(l.debit) - SUM(l.credit) AS difference, COUNT(*) AS line_count
FROM fin.journals j JOIN fin.journal_lines l USING (journal_id)
GROUP BY j.journal_id;

-- ---------------------------------------------------------------- exceptions
CREATE OR REPLACE VIEW rpt.exceptions AS
SELECT e.exception_id, e.period, e.check_code, c.check_name, e.severity, e.pool_code, e.cc_code,
       e.owner_role, e.root_cause, e.description, e.amount_at_risk, e.status, e.escalated,
       e.opened_at, e.due_at, e.resolved_at, e.closed_at,
       ref.working_days_between(e.opened_at::DATE, COALESCE(e.resolved_at, e.opened_at)::DATE) AS working_days_to_resolve,
       (e.resolved_at IS NOT NULL AND e.resolved_at <= e.due_at) AS resolved_within_sla,
       (SELECT MIN(ev.event_at) FROM ops.exception_events ev
         WHERE ev.exception_id = e.exception_id AND ev.event_type = 'Escalated') AS escalated_at
FROM ops.exceptions e JOIN ops.check_catalogue c USING (check_code);

-- ---------------------------------------------------------------- controls and reconciliations
CREATE OR REPLACE VIEW rpt.control_results AS
SELECT period, control_id, control_name, result,
       (result IN ('Pass', 'Approved')) AS passed,
       evidence_ref, detail, prepared_by, reviewer_status, recorded_at, run_id
FROM ops.control_results;

CREATE OR REPLACE VIEW rpt.reconciliations AS
SELECT r.run_id, ru.run_type, ru.run_at, r.period, r.recon_id, r.recon_name, r.pool_code,
       r.left_value, r.right_value, r.difference, r.result
FROM fin.reconciliations r JOIN ops.runs ru USING (run_id);

-- ---------------------------------------------------------------- monthly KPIs
CREATE OR REPLACE VIEW rpt.monthly_kpis AS
SELECT p.period,
       (SELECT SUM(pool_cost)            FROM rpt.allocation_summary s WHERE s.period = p.period) AS shared_cost,
       (SELECT SUM(allocated_by_close)   FROM rpt.allocation_summary s WHERE s.period = p.period) AS allocated_by_close,
       (SELECT SUM(unallocated_at_close) FROM rpt.allocation_summary s WHERE s.period = p.period) AS unallocated_at_close,
       (SELECT COUNT(*) FROM ops.exceptions e WHERE e.period = p.period)                         AS exceptions_opened,
       (SELECT COUNT(*) FROM ops.exceptions e WHERE e.period = p.period AND e.escalated)         AS exceptions_escalated,
       (SELECT COUNT(*) FROM ops.control_results c WHERE c.period = p.period
          AND c.result IN ('Pass', 'Approved'))                                                  AS controls_passed,
       (SELECT COUNT(*) FROM ops.control_results c WHERE c.period = p.period)                    AS controls_total,
       (SELECT COUNT(*) FROM fin.reconciliations r WHERE r.period = p.period AND r.result = 'Fail') AS recon_failures,
       (SELECT COUNT(*) FROM ops.runs r WHERE r.period = p.period AND r.run_type <> 'Close')     AS runs,
       p.status, p.closed_at
FROM ref.periods p;
