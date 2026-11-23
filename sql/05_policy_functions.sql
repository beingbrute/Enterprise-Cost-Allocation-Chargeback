-- =====================================================================
-- 05  Expense policy checks P01-P10
-- pol.run_checks(run_id, period) checks every claim paid in the period against the policy
-- version in force on each expense date, and writes one row per claim and failed check
-- to pol.check_results. Rules: docs/expense_policy.md
-- =====================================================================

-- Policy limit in force on a date
CREATE OR REPLACE FUNCTION pol.rule_at(p_key TEXT, p_date DATE)
RETURNS NUMERIC LANGUAGE sql STABLE AS $$
    SELECT value FROM pol.policy_rules
    WHERE rule_key = p_key AND p_date BETWEEN valid_from AND valid_to
$$;

CREATE OR REPLACE FUNCTION pol.version_at(p_date DATE)
RETURNS INT LANGUAGE sql STABLE AS $$
    SELECT MAX(version) FROM pol.policy_rules WHERE p_date BETWEEN valid_from AND valid_to
$$;

-- Approval level needed for a claim total (1 line manager, 2 department head, 3 finance director)
CREATE OR REPLACE FUNCTION pol.level_needed(p_total NUMERIC)
RETURNS INT LANGUAGE sql STABLE AS $$
    SELECT approval_level FROM pol.approval_matrix WHERE p_total BETWEEN min_total AND max_total
$$;

-- Approval level an approver holds for a claimant: 0 if the approver is the claimant
-- or not in the claimant's approval chain
CREATE OR REPLACE FUNCTION pol.approver_level(p_claimant TEXT, p_approver TEXT)
RETURNS INT LANGUAGE sql STABLE AS $$
    SELECT CASE
             WHEN p_approver = p_claimant THEN 0
             WHEN p_approver NOT IN (COALESCE(e.line_manager_id, ''), e.dept_head_id, e.finance_director_id) THEN 0
             ELSE CASE a.role WHEN 'Manager' THEN 1 WHEN 'Department head' THEN 2
                              WHEN 'Finance director' THEN 3 ELSE 0 END
           END
    FROM pol.employees e, pol.employees a
    WHERE e.employee_id = p_claimant AND a.employee_id = p_approver
$$;

-- Is there a valid exception for this employee, check and date?
CREATE OR REPLACE FUNCTION pol.has_exception(p_employee TEXT, p_check TEXT, p_date DATE, p_ref TEXT DEFAULT NULL)
RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT EXISTS (
        SELECT 1 FROM pol.exception_register x
        WHERE x.employee_id = p_employee AND x.rule_code = p_check
          AND (p_ref IS NULL OR x.exception_id = p_ref)
          AND p_date BETWEEN COALESCE(x.valid_from, p_date) AND COALESCE(x.valid_to, p_date))
$$;

CREATE OR REPLACE FUNCTION pol.run_checks(p_run_id INT, p_period TEXT)
RETURNS INT LANGUAGE plpgsql AS $$
DECLARE
    n INT;
BEGIN
    DROP TABLE IF EXISTS tmp_lines, tmp_hits;
    CREATE TEMP TABLE tmp_lines AS
    SELECT l.*, c.employee_id, c.submitted_date, c.approved_by, c.claim_total,
           pol.version_at(l.expense_date) AS ver
    FROM pol.claim_lines l JOIN pol.claims c USING (claim_id)
    WHERE to_char(c.paid_date, 'YYYY-MM') = p_period;

    CREATE TEMP TABLE tmp_hits (claim_id TEXT, check_code TEXT, amount NUMERIC, ver INT, detail TEXT);

    -- P01 receipt missing
    INSERT INTO tmp_hits
    SELECT claim_id, 'P01', amount, ver, 'No receipt for ' || category || ' ' || amount
    FROM tmp_lines WHERE receipt_id IS NULL AND amount >= pol.rule_at('receipt_min', expense_date);

    -- P02 hotel over the nightly cap (amount in question = the part over the cap)
    INSERT INTO tmp_hits
    SELECT claim_id, 'P02', amount - cap * nights, ver,
           format('%s, %s nights at %s a night; cap %s (v%s)', city, nights, round(amount / nights, 2), cap, ver)
    FROM (SELECT t.*, pol.rule_at(CASE WHEN h.city IS NULL THEN 'hotel_standard_night' ELSE 'hotel_high_cost_night' END,
                                  expense_date) AS cap
          FROM tmp_lines t LEFT JOIN pol.high_cost_cities h ON h.city = t.city
          WHERE category = 'Hotel') x
    WHERE amount > cap * nights;

    -- P03 meals over the daily cap (all of the employee's meals that day, in claims paid so far)
    INSERT INTO tmp_hits
    SELECT d.claim_id, 'P03', m.total - pol.rule_at('meals_day', d.expense_date), d.ver,
           format('Meals %s on %s; cap %s', m.total, d.expense_date, pol.rule_at('meals_day', d.expense_date))
    FROM (SELECT employee_id, expense_date, MAX(claim_id) AS claim_id, MAX(ver) AS ver
          FROM tmp_lines WHERE category = 'Meals' GROUP BY employee_id, expense_date) d
    CROSS JOIN LATERAL (SELECT SUM(l.amount) AS total FROM pol.claim_lines l JOIN pol.claims c USING (claim_id)
                        WHERE c.employee_id = d.employee_id AND l.expense_date = d.expense_date
                          AND l.category = 'Meals' AND to_char(c.paid_date, 'YYYY-MM') <= p_period) m
    WHERE m.total > pol.rule_at('meals_day', d.expense_date);

    -- P04 business class or upgrade without a valid pre-approval
    INSERT INTO tmp_hits
    SELECT claim_id, 'P04', amount, ver, travel_class || ' fare ' || amount || ' without pre-approval'
    FROM tmp_lines
    WHERE category = 'Airfare' AND COALESCE(travel_class, 'Economy') <> 'Economy'
      AND NOT (pre_approval_ref IS NOT NULL AND pol.has_exception(employee_id, 'P04', expense_date, pre_approval_ref));

    -- P05 client entertainment over the per-attendee limit, or no attendees / no client
    INSERT INTO tmp_hits
    SELECT claim_id, 'P05',
           CASE WHEN COALESCE(attendees, 0) = 0 OR NOT client_present THEN amount
                ELSE amount - attendees * pol.rule_at('entertainment_per_attendee', expense_date) END,
           ver,
           CASE WHEN COALESCE(attendees, 0) = 0 OR NOT client_present THEN 'No attendees or client recorded'
                ELSE format('%s for %s attendees (%s each); limit %s', amount, attendees,
                            round(amount / attendees, 2), pol.rule_at('entertainment_per_attendee', expense_date)) END
    FROM tmp_lines
    WHERE category = 'Client entertainment'
      AND (COALESCE(attendees, 0) = 0 OR NOT client_present
           OR amount > attendees * pol.rule_at('entertainment_per_attendee', expense_date));

    -- P06 duplicate of a line in an earlier claim (same employee, date, merchant, amount)
    INSERT INTO tmp_hits
    SELECT t.claim_id, 'P06', t.amount, t.ver, 'Duplicate of ' || MIN(c2.claim_id)
    FROM tmp_lines t
    JOIN pol.claim_lines l2 ON l2.expense_date = t.expense_date AND l2.merchant = t.merchant AND l2.amount = t.amount
    JOIN pol.claims c2 ON c2.claim_id = l2.claim_id AND c2.employee_id = t.employee_id AND c2.claim_id < t.claim_id
    GROUP BY t.claim_id, t.line_id, t.amount, t.ver;

    -- P07 split claim: same employee, merchant and date over several claims; together they need
    -- a higher approval level than any of the approvals obtained
    INSERT INTO tmp_hits
    SELECT d.claim_id, 'P07', g.total, d.ver,
           format('%s claims at %s on %s total %s; needs level %s, highest approval level %s',
                  g.n_claims, d.merchant, d.expense_date, g.total, pol.level_needed(g.total), g.best_level)
    FROM (SELECT employee_id, merchant, expense_date, MAX(claim_id) AS claim_id, MAX(ver) AS ver
          FROM tmp_lines GROUP BY employee_id, merchant, expense_date) d
    CROSS JOIN LATERAL (SELECT COUNT(DISTINCT c.claim_id) AS n_claims, SUM(l.amount) AS total,
                               MAX(pol.approver_level(c.employee_id, c.approved_by)) AS best_level
                        FROM pol.claim_lines l JOIN pol.claims c USING (claim_id)
                        WHERE c.employee_id = d.employee_id AND l.merchant = d.merchant
                          AND l.expense_date = d.expense_date AND to_char(c.paid_date, 'YYYY-MM') <= p_period) g
    WHERE g.n_claims > 1 AND pol.level_needed(g.total) > g.best_level
      AND NOT EXISTS (SELECT 1 FROM tmp_hits h WHERE h.claim_id = d.claim_id AND h.check_code = 'P06');

    -- P08 approved at the wrong level or by the claimant (one row per claim)
    INSERT INTO tmp_hits
    SELECT claim_id, 'P08', claim_total, MAX(ver),
           CASE WHEN approved_by = employee_id THEN 'Approved by the claimant'
                ELSE format('Total %s needs level %s; approver level %s', claim_total,
                            pol.level_needed(claim_total), pol.approver_level(employee_id, approved_by)) END
    FROM tmp_lines
    GROUP BY claim_id, claim_total, approved_by, employee_id
    HAVING pol.approver_level(employee_id, approved_by) < pol.level_needed(claim_total);

    -- P09 submitted after the deadline in force on the expense date
    INSERT INTO tmp_hits
    SELECT claim_id, 'P09', SUM(amount), MAX(ver),
           format('Submitted %s days after the expense; limit %s (v%s)',
                  MAX(submitted_date - expense_date), MIN(pol.rule_at('submit_within_days', expense_date)), MAX(ver))
    FROM tmp_lines
    WHERE submitted_date - expense_date > pol.rule_at('submit_within_days', expense_date)
      AND NOT pol.has_exception(employee_id, 'P09', expense_date)
    GROUP BY claim_id;

    -- P10 not reimbursable: alcohol with no client present
    INSERT INTO tmp_hits
    SELECT claim_id, 'P10', amount, ver, 'Alcohol with no client present, ' || amount
    FROM tmp_lines WHERE alcohol AND NOT client_present;

    INSERT INTO pol.check_results (run_id, claim_id, check_code, amount_in_question, policy_version, detail)
    SELECT p_run_id, claim_id, check_code, SUM(amount), MAX(ver), string_agg(detail, '; ')
    FROM tmp_hits GROUP BY claim_id, check_code;

    UPDATE pol.review_runs SET claims_checked = (SELECT COUNT(DISTINCT claim_id) FROM tmp_lines) WHERE run_id = p_run_id;
    SELECT COUNT(*) INTO n FROM pol.check_results WHERE run_id = p_run_id;
    DROP TABLE tmp_lines; DROP TABLE tmp_hits;
    RETURN n;
END $$;
