-- =====================================================================
-- 06  Reporting views for the expense policy module (schema rpt)
-- Built from recorded review results only. Run after sql/03_reporting_views.sql.
-- =====================================================================

-- Employees with their cost centre, department and business unit (as at 1 Jan 2025)
CREATE OR REPLACE VIEW rpt.dim_employee AS
SELECT e.employee_id, e.role, e.cc_code, e.dept_code, e.bu_code, b.bu_name,
       e.line_manager_id, e.dept_head_id, e.finance_director_id
FROM pol.employees e JOIN ref.business_units b USING (bu_code);

-- One row per claim: when it was paid, how much, and whether it raised a case
CREATE OR REPLACE VIEW rpt.policy_claims AS
SELECT c.claim_id, to_char(c.paid_date, 'YYYY-MM') AS period, c.employee_id, e.bu_code,
       c.submitted_date, c.approved_by, c.paid_date, c.claim_total,
       (SELECT COUNT(*) FROM pol.claim_lines l WHERE l.claim_id = c.claim_id) AS line_count,
       (SELECT COUNT(*) FROM pol.cases k WHERE k.claim_id = c.claim_id) AS case_count
FROM pol.claims c JOIN pol.employees e USING (employee_id);

-- One row per case, with the timeline and SLA results
CREATE OR REPLACE VIEW rpt.policy_cases AS
SELECT k.case_id, k.period, k.claim_id, k.check_code, cc.check_name, k.severity, k.employee_id, e.bu_code,
       k.amount_in_question, k.detail, k.owner_role, k.status, k.decision, k.disallowed, k.recovered,
       k.disallowed - k.recovered AS outstanding, k.evidence, k.escalated,
       k.opened_at, k.triaged_at, k.decision_due_at, k.decided_at, k.remediation_due_at, k.closed_at,
       ref.working_days_between(k.opened_at::DATE, COALESCE(k.decided_at, k.opened_at)::DATE) AS working_days_to_decide,
       (k.decided_at IS NOT NULL AND k.decided_at <= k.decision_due_at) AS decided_within_sla,
       (SELECT MIN(ev.event_at) FROM pol.case_events ev
         WHERE ev.case_id = k.case_id AND ev.event_type = 'Escalated') AS escalated_at,
       EXISTS (SELECT 1 FROM pol.case_events ev
               WHERE ev.case_id = k.case_id AND ev.event_type = 'Recovery failed') AS reopened,
       EXISTS (SELECT 1 FROM pol.repeat_reports r
               WHERE r.employee_id = k.employee_id AND r.reported_at = k.opened_at) AS repeat_reported
FROM pol.cases k
JOIN pol.check_catalogue cc USING (check_code)
JOIN pol.employees e USING (employee_id);

CREATE OR REPLACE VIEW rpt.policy_case_events AS
SELECT case_id, event_at, event_type, detail FROM pol.case_events;

CREATE OR REPLACE VIEW rpt.policy_governance AS
SELECT period, control_id, control_name, result, (result = 'Pass') AS passed,
       evidence_ref, detail, prepared_by, reviewer_status, recorded_at
FROM pol.governance_results;

CREATE OR REPLACE VIEW rpt.policy_exception_register AS
SELECT exception_id, employee_id, rule_code, reason, approved_by, valid_from, valid_to, source, case_id, recorded_at
FROM pol.exception_register;

-- One row per month
CREATE OR REPLACE VIEW rpt.policy_monthly AS
SELECT p.period,
       (SELECT COUNT(*) FROM pol.claims c WHERE to_char(c.paid_date, 'YYYY-MM') = p.period) AS claims_paid,
       (SELECT COALESCE(SUM(c.claim_total), 0) FROM pol.claims c WHERE to_char(c.paid_date, 'YYYY-MM') = p.period) AS amount_paid,
       (SELECT claims_checked FROM pol.review_runs r WHERE r.period = p.period) AS claims_checked,
       (SELECT COUNT(*) FROM pol.cases k WHERE k.period = p.period) AS cases_opened,
       (SELECT COUNT(*) FROM pol.cases k WHERE k.period = p.period AND k.escalated) AS cases_escalated,
       (SELECT COALESCE(SUM(k.disallowed), 0) FROM pol.cases k WHERE k.period = p.period) AS disallowed,
       (SELECT COALESCE(SUM(k.recovered), 0) FROM pol.cases k WHERE k.period = p.period) AS recovered,
       (SELECT COUNT(*) FROM pol.governance_results g WHERE g.period = p.period AND g.result = 'Pass') AS controls_passed,
       (SELECT COUNT(*) FROM pol.governance_results g WHERE g.period = p.period) AS controls_total
FROM ref.periods p;
