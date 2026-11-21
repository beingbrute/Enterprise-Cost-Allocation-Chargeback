-- =====================================================================
-- 02  Functions: hierarchy lookup, hierarchy changes, validation checks,
--     allocation engine, reconciliations
-- Called by src/pipeline.py for each run.
-- =====================================================================

-- ---------------------------------------------------------------------
-- Hierarchy as at a date: cost centre -> department -> business unit
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ref.mapping_at(p_date DATE)
RETURNS TABLE (cc_code TEXT, cc_name TEXT, dept_code TEXT, bu_code TEXT,
               remote_only BOOLEAN, is_active BOOLEAN)
LANGUAGE sql STABLE AS $$
    SELECT c.cc_code, c.cc_name, c.dept_code, d.bu_code, c.remote_only, c.is_active
    FROM ref.cost_centres c
    LEFT JOIN ref.departments d
           ON d.dept_code = c.dept_code
          AND p_date BETWEEN d.valid_from AND d.valid_to
    WHERE p_date BETWEEN c.valid_from AND c.valid_to;
$$;

-- Working days between two dates (weekends and holidays excluded)
CREATE OR REPLACE FUNCTION ref.working_days_between(p_from DATE, p_to DATE)
RETURNS INT LANGUAGE sql STABLE AS $$
    SELECT COUNT(*)::INT
    FROM GENERATE_SERIES(p_from + 1, p_to, INTERVAL '1 day') g(d)
    WHERE EXTRACT(ISODOW FROM g.d) < 6
      AND g.d::DATE NOT IN (SELECT holiday_date FROM ref.holidays);
$$;

-- ---------------------------------------------------------------------
-- Apply a hierarchy change file. All changes in the file are checked
-- first; if any is invalid, none is applied and V07 is recorded.
-- Applying the same request twice does nothing (idempotent).
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops.apply_hierarchy_file(p_run_id INT, p_file_id INT)
RETURNS INT LANGUAGE plpgsql AS $$
DECLARE
    r        RECORD;
    v_bad    INT := 0;
    v_done   INT := 0;
BEGIN
    -- 1. validate every change in the file
    FOR r IN SELECT * FROM src.hierarchy_changes WHERE file_id = p_file_id ORDER BY line_no LOOP
        IF r.change_type IN ('MOVE', 'OPEN')
           AND NOT EXISTS (SELECT 1 FROM ref.departments d
                           WHERE d.dept_code = r.new_dept AND r.effective_date BETWEEN d.valid_from AND d.valid_to) THEN
            INSERT INTO ops.check_results VALUES (p_run_id, 'V07', NULL, r.object_code,
                FORMAT('%s: %s mapped to unknown department %s', r.request_id, r.object_code, r.new_dept), NULL, TRUE);
            v_bad := v_bad + 1;
        ELSIF r.change_type = 'DEPT_MOVE'
           AND NOT EXISTS (SELECT 1 FROM ref.business_units b WHERE b.bu_code = r.new_bu) THEN
            INSERT INTO ops.check_results VALUES (p_run_id, 'V07', NULL, r.object_code,
                FORMAT('%s: department %s moved to unknown business unit %s', r.request_id, r.object_code, r.new_bu), NULL, TRUE);
            v_bad := v_bad + 1;
        ELSIF r.change_type IN ('MOVE', 'CLOSE', 'RENAME')
           AND NOT EXISTS (SELECT 1 FROM ref.cost_centres c
                           WHERE c.cc_code = r.object_code AND r.effective_date BETWEEN c.valid_from AND c.valid_to) THEN
            INSERT INTO ops.check_results VALUES (p_run_id, 'V07', NULL, r.object_code,
                FORMAT('%s: cost centre %s does not exist on %s', r.request_id, r.object_code, r.effective_date), NULL, TRUE);
            v_bad := v_bad + 1;
        END IF;
    END LOOP;

    IF v_bad > 0 THEN
        RETURN 0;
    END IF;

    -- 2. apply valid changes (skip any already applied)
    FOR r IN SELECT * FROM src.hierarchy_changes WHERE file_id = p_file_id ORDER BY line_no LOOP
        CONTINUE WHEN EXISTS (SELECT 1 FROM ref.cost_centres WHERE change_ref = r.request_id)
                   OR EXISTS (SELECT 1 FROM ref.departments  WHERE change_ref = r.request_id);

        IF r.change_type = 'DEPT_MOVE' THEN
            INSERT INTO ref.departments (dept_code, dept_name, bu_code, valid_from, valid_to, change_ref)
            SELECT dept_code, dept_name, r.new_bu, r.effective_date, valid_to, r.request_id
            FROM ref.departments
            WHERE dept_code = r.object_code AND r.effective_date BETWEEN valid_from AND valid_to;
            UPDATE ref.departments SET valid_to = r.effective_date - 1
            WHERE dept_code = r.object_code AND r.effective_date BETWEEN valid_from AND valid_to
              AND COALESCE(change_ref, '') <> r.request_id;

        ELSIF r.change_type = 'OPEN' THEN
            INSERT INTO ref.cost_centres (cc_code, cc_name, dept_code, remote_only, is_active, valid_from, change_ref)
            VALUES (r.object_code, r.new_name, r.new_dept, FALSE, TRUE, r.effective_date, r.request_id);

        ELSE  -- MOVE, CLOSE, RENAME: close the current row and start a new version
            INSERT INTO ref.cost_centres (cc_code, cc_name, dept_code, remote_only, is_active, valid_from, valid_to, change_ref)
            SELECT cc_code,
                   CASE WHEN r.change_type = 'RENAME' THEN r.new_name ELSE cc_name END,
                   CASE WHEN r.change_type = 'MOVE' THEN r.new_dept ELSE dept_code END,
                   remote_only,
                   CASE WHEN r.change_type = 'CLOSE' THEN FALSE ELSE is_active END,
                   r.effective_date, valid_to, r.request_id
            FROM ref.cost_centres
            WHERE cc_code = r.object_code AND r.effective_date BETWEEN valid_from AND valid_to;
            UPDATE ref.cost_centres SET valid_to = r.effective_date - 1
            WHERE cc_code = r.object_code AND r.effective_date BETWEEN valid_from AND valid_to
              AND COALESCE(change_ref, '') <> r.request_id;
        END IF;
        v_done := v_done + 1;
    END LOOP;
    RETURN v_done;
END;
$$;

-- ---------------------------------------------------------------------
-- Validate the expense and volume files of one pool for a run.
-- Writes failed checks to ops.check_results. Returns TRUE if the pool
-- has no blocking failure.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION ops.validate_pool(p_run_id INT, p_period TEXT, p_pool TEXT,
                                             p_expense_file INT, p_volume_file INT)
RETURNS BOOLEAN LANGUAGE plpgsql AS $$
DECLARE
    v_date    DATE;
    v_driver  TEXT;
    v_excl    BOOLEAN;
    v_cost    NUMERIC(18,2);
    v_units   NUMERIC;
    v_blocked BOOLEAN := FALSE;
BEGIN
    SELECT period_start INTO v_date FROM ref.periods WHERE period = p_period;
    SELECT driver, excludes_remote_only INTO v_driver, v_excl
    FROM ref.allocation_policies WHERE pool_code = p_pool AND v_date BETWEEN valid_from AND valid_to;

    -- Expense file: row count and control total against the manifest
    SELECT COALESCE(SUM(amount), 0) INTO v_cost FROM src.expense_lines WHERE file_id = p_expense_file;

    INSERT INTO ops.check_results
    SELECT p_run_id, 'V10', p_pool, NULL,
           FORMAT('%s: %s rows loaded, manifest says %s', f.file_name, COUNT(l.line_no), f.row_count),
           f.control_total, TRUE
    FROM src.files f LEFT JOIN src.expense_lines l ON l.file_id = f.file_id
    WHERE f.file_id = p_expense_file
    GROUP BY f.file_name, f.row_count, f.control_total
    HAVING COUNT(l.line_no) <> f.row_count;

    INSERT INTO ops.check_results
    SELECT p_run_id, 'V01', p_pool, NULL,
           FORMAT('%s: loaded total %s, control total %s, difference %s',
                  f.file_name, v_cost, f.control_total, f.control_total - v_cost),
           f.control_total, TRUE
    FROM src.files f
    WHERE f.file_id = p_expense_file AND f.control_total <> v_cost;

    -- Volume file
    CREATE TEMP TABLE IF NOT EXISTS tmp_vol (cc_code TEXT, quantity NUMERIC) ON COMMIT DROP;
    TRUNCATE tmp_vol;
    INSERT INTO tmp_vol SELECT cc_code, quantity FROM src.volume_lines
    WHERE file_id = p_volume_file AND driver = v_driver;

    -- V02 unknown cost centre (never existed)
    INSERT INTO ops.check_results
    SELECT DISTINCT p_run_id, 'V02', p_pool, v.cc_code,
           FORMAT('Volume submitted for unknown cost centre %s', v.cc_code), v_cost, TRUE
    FROM tmp_vol v
    WHERE NOT EXISTS (SELECT 1 FROM ref.cost_centres c WHERE c.cc_code = v.cc_code);

    -- V06 cost centre exists but is not active in the period
    INSERT INTO ops.check_results
    SELECT DISTINCT p_run_id, 'V06', p_pool, v.cc_code,
           FORMAT('Volume submitted for inactive cost centre %s', v.cc_code), v_cost, TRUE
    FROM tmp_vol v
    WHERE EXISTS (SELECT 1 FROM ref.cost_centres c WHERE c.cc_code = v.cc_code)
      AND NOT EXISTS (SELECT 1 FROM ref.mapping_at(v_date) m WHERE m.cc_code = v.cc_code AND m.is_active);

    -- V04 negative volume
    INSERT INTO ops.check_results
    SELECT p_run_id, 'V04', p_pool, v.cc_code,
           FORMAT('Negative %s for %s: %s', v_driver, v.cc_code, v.quantity), v_cost, TRUE
    FROM tmp_vol v WHERE v.quantity < 0;

    -- V05 duplicate rows for the same cost centre
    INSERT INTO ops.check_results
    SELECT p_run_id, 'V05', p_pool, v.cc_code,
           FORMAT('%s rows for %s (expected 1)', COUNT(*), v.cc_code), v_cost, TRUE
    FROM tmp_vol v GROUP BY v.cc_code HAVING COUNT(*) > 1;

    -- V03 missing volume for an active, eligible cost centre
    INSERT INTO ops.check_results
    SELECT p_run_id, 'V03', p_pool, m.cc_code,
           FORMAT('No %s submitted for active cost centre %s', v_driver, m.cc_code), v_cost, TRUE
    FROM ref.mapping_at(v_date) m
    WHERE m.is_active AND m.bu_code <> 'BU90'
      AND NOT (v_excl AND m.remote_only)
      AND NOT EXISTS (SELECT 1 FROM tmp_vol v WHERE v.cc_code = m.cc_code);

    -- V09 zero total units
    SELECT COALESCE(SUM(quantity), 0) INTO v_units FROM tmp_vol;
    IF v_units = 0 THEN
        INSERT INTO ops.check_results VALUES (p_run_id, 'V09', p_pool, NULL,
            FORMAT('Total %s is zero', v_driver), v_cost, TRUE);
    END IF;

    SELECT EXISTS (SELECT 1 FROM ops.check_results
                   WHERE run_id = p_run_id AND pool_code = p_pool AND blocks_pool)
      INTO v_blocked;
    RETURN NOT v_blocked;
END;
$$;

-- ---------------------------------------------------------------------
-- Allocation engine for one pool and one source month.
-- Exact shares, then cent-level rounding with the largest-remainder
-- method so the lines add back to the pool cost exactly.
-- Returns the new allocation_id.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fin.allocate_pool(p_run_id INT, p_period TEXT, p_pool TEXT,
                                             p_expense_file INT, p_volume_file INT,
                                             p_allocation_version INT, p_at TIMESTAMP)
RETURNS INT LANGUAGE plpgsql AS $$
DECLARE
    v_date     DATE;
    v_policy   RECORD;
    v_cost     NUMERIC(18,2);
    v_units    NUMERIC;
    v_hash     TEXT;
    v_id       INT;
BEGIN
    SELECT period_start INTO v_date FROM ref.periods WHERE period = p_period;
    SELECT * INTO v_policy FROM ref.allocation_policies
    WHERE pool_code = p_pool AND v_date BETWEEN valid_from AND valid_to;
    SELECT SUM(amount) INTO v_cost FROM src.expense_lines WHERE file_id = p_expense_file;

    CREATE TEMP TABLE IF NOT EXISTS tmp_alloc (cc_code TEXT, dept_code TEXT, bu_code TEXT, units NUMERIC) ON COMMIT DROP;
    TRUNCATE tmp_alloc;
    INSERT INTO tmp_alloc
    SELECT v.cc_code, m.dept_code, m.bu_code, v.quantity
    FROM src.volume_lines v
    JOIN ref.mapping_at(v_date) m ON m.cc_code = v.cc_code
    WHERE v.file_id = p_volume_file
      AND v.driver = v_policy.driver
      AND m.is_active AND m.bu_code <> 'BU90'
      AND NOT (v_policy.excludes_remote_only AND m.remote_only);

    SELECT SUM(units) INTO v_units FROM tmp_alloc;

    -- Fingerprint of everything the result depends on
    SELECT MD5(v_cost::TEXT || '|' || v_policy.version || '|' ||
               STRING_AGG(cc_code || ':' || bu_code || ':' || units::TEXT, ',' ORDER BY cc_code))
      INTO v_hash FROM tmp_alloc;

    INSERT INTO fin.allocations (run_id, source_period, pool_code, policy_version, driver,
                                 allocation_version, pool_cost, total_units, rate, input_hash, created_at)
    VALUES (p_run_id, p_period, p_pool, v_policy.version, v_policy.driver,
            p_allocation_version, v_cost, v_units, ROUND(v_cost / v_units, 6), v_hash, p_at)
    RETURNING allocation_id INTO v_id;

    WITH exact AS (
        SELECT cc_code, dept_code, bu_code, units,
               units / v_units                 AS share,
               v_cost * units / v_units        AS exact_amount
        FROM tmp_alloc
    ), floored AS (
        SELECT *, TRUNC(exact_amount, 2) AS base,
               exact_amount - TRUNC(exact_amount, 2) AS remainder
        FROM exact
    ), ranked AS (
        SELECT *, ROW_NUMBER() OVER (ORDER BY remainder DESC, cc_code) AS rnk,
               ROUND((v_cost - SUM(base) OVER ()) * 100) AS cents_left
        FROM floored
    )
    INSERT INTO fin.allocation_lines (allocation_id, cc_code, dept_code, bu_code, units, share,
                                      exact_amount, allocated_amount)
    SELECT v_id, cc_code, dept_code, bu_code, units, share, exact_amount,
           base + CASE WHEN rnk <= cents_left THEN 0.01 ELSE 0 END
    FROM ranked;

    RETURN v_id;
END;
$$;

-- ---------------------------------------------------------------------
-- Reconciliations for a run (R01-R08). R09 (Excel) and R10 (Power BI)
-- are checked outside the database.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fin.run_reconciliations(p_run_id INT)
RETURNS INT LANGUAGE plpgsql AS $$
DECLARE
    v_period  TEXT;
    v_post    TEXT;
    v_at      TIMESTAMP;
    v_date    DATE;
    v_fails   INT;
BEGIN
    SELECT period, COALESCE(posting_period, period), run_at INTO v_period, v_post, v_at
    FROM ops.runs WHERE run_id = p_run_id;
    SELECT period_start INTO v_date FROM ref.periods WHERE period = v_period;

    -- R01 source expense file control total vs loaded lines (per pool, files used in this run)
    INSERT INTO fin.reconciliations
    SELECT p_run_id, v_period, 'R01', 'Source control total vs loaded expense', f.pool_code,
           f.control_total, COALESCE(SUM(l.amount), 0), f.control_total - COALESCE(SUM(l.amount), 0),
           CASE WHEN f.control_total = COALESCE(SUM(l.amount), 0) THEN 'Pass' ELSE 'Fail' END, v_at
    FROM ops.run_files rf
    JOIN src.files f ON f.file_id = rf.file_id AND f.file_type = 'EXPENSE'
    LEFT JOIN src.expense_lines l ON l.file_id = f.file_id
    WHERE rf.run_id = p_run_id
    GROUP BY f.pool_code, f.control_total;

    -- R02 pool cost = allocated + unallocated (per pool, current allocation for the source month)
    INSERT INTO fin.reconciliations
    SELECT p_run_id, v_period, 'R02', 'Pool cost = allocated + unallocated', rp.pool_code,
           rp.pool_cost,
           COALESCE(cur.allocated, 0) + CASE WHEN cur.allocated IS NULL THEN rp.pool_cost ELSE 0 END,
           rp.pool_cost - (COALESCE(cur.allocated, 0) + CASE WHEN cur.allocated IS NULL THEN rp.pool_cost ELSE 0 END),
           CASE WHEN rp.pool_cost = COALESCE(cur.allocated, 0) + CASE WHEN cur.allocated IS NULL THEN rp.pool_cost ELSE 0 END
                THEN 'Pass' ELSE 'Fail' END, v_at
    FROM ops.run_pools rp
    LEFT JOIN LATERAL (
        SELECT SUM(al.allocated_amount) AS allocated
        FROM fin.allocations a JOIN fin.allocation_lines al USING (allocation_id)
        WHERE a.allocation_id = (SELECT j.allocation_id FROM fin.journals j
                                 JOIN fin.allocations a2 USING (allocation_id)
                                 WHERE a2.source_period = v_period AND a2.pool_code = rp.pool_code
                                   AND j.status = 'Posted' AND j.journal_type IN ('Allocation', 'Replacement', 'Catch-up')
                                 ORDER BY a2.allocation_version DESC LIMIT 1)
    ) cur ON TRUE
    WHERE rp.run_id = p_run_id AND rp.pool_cost IS NOT NULL;

    -- R03 allocation detail vs allocation header (allocations created in this run)
    INSERT INTO fin.reconciliations
    SELECT p_run_id, v_period, 'R03', 'Allocation detail vs summary', a.pool_code,
           a.pool_cost, SUM(al.allocated_amount), a.pool_cost - SUM(al.allocated_amount),
           CASE WHEN a.pool_cost = SUM(al.allocated_amount) THEN 'Pass' ELSE 'Fail' END, v_at
    FROM fin.allocations a JOIN fin.allocation_lines al USING (allocation_id)
    WHERE a.run_id = p_run_id
    GROUP BY a.allocation_id, a.pool_code, a.pool_cost;

    -- R04 debits = credits for every journal posted in the posting period
    INSERT INTO fin.reconciliations
    SELECT p_run_id, v_period, 'R04', 'Ledger debits vs credits (posting period)', 'ALL',
           COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0), COALESCE(SUM(debit) - SUM(credit), 0),
           CASE WHEN COALESCE(SUM(debit), 0) = COALESCE(SUM(credit), 0) THEN 'Pass' ELSE 'Fail' END, v_at
    FROM fin.ledger WHERE posting_period = v_post;

    -- R05 posted allocated expense vs allocation schedule (journals created in this run)
    INSERT INTO fin.reconciliations
    SELECT p_run_id, v_period, 'R05', 'Posted journal vs allocation schedule', j.pool_code,
           (SELECT SUM(allocated_amount) FROM fin.allocation_lines WHERE allocation_id = j.allocation_id),
           SUM(lg.debit),
           (SELECT SUM(allocated_amount) FROM fin.allocation_lines WHERE allocation_id = j.allocation_id) - SUM(lg.debit),
           CASE WHEN (SELECT SUM(allocated_amount) FROM fin.allocation_lines WHERE allocation_id = j.allocation_id)
                     = SUM(lg.debit) THEN 'Pass' ELSE 'Fail' END, v_at
    FROM fin.journals j JOIN fin.ledger lg USING (journal_id)
    JOIN ref.gl_accounts g ON g.account = lg.account AND g.account_type = 'Allocated expense'
    WHERE j.created_run_id = p_run_id AND j.journal_type IN ('Allocation', 'Replacement', 'Catch-up')
    GROUP BY j.journal_id, j.pool_code, j.allocation_id;

    -- R06 company-wide expense unchanged by allocation journals (posting period)
    INSERT INTO fin.reconciliations
    SELECT p_run_id, v_period, 'R06', 'Company-wide expense before vs after allocation', 'ALL',
           SUM(CASE WHEN j.journal_type = 'Source' THEN lg.debit - lg.credit ELSE 0 END),
           SUM(lg.debit - lg.credit),
           SUM(CASE WHEN j.journal_type = 'Source' THEN lg.debit - lg.credit ELSE 0 END) - SUM(lg.debit - lg.credit),
           CASE WHEN SUM(CASE WHEN j.journal_type = 'Source' THEN lg.debit - lg.credit ELSE 0 END)
                     = SUM(lg.debit - lg.credit) THEN 'Pass' ELSE 'Fail' END, v_at
    FROM fin.ledger lg
    JOIN fin.journals j USING (journal_id)
    JOIN ref.gl_accounts g ON g.account = lg.account AND g.account_type <> 'Liability'
    WHERE lg.posting_period = v_post;

    -- R07 every active cost centre maps to exactly one department and business unit
    INSERT INTO fin.reconciliations
    SELECT p_run_id, v_period, 'R07', 'Cost centres with invalid or double mapping', 'ALL',
           0, COUNT(*), -COUNT(*), CASE WHEN COUNT(*) = 0 THEN 'Pass' ELSE 'Fail' END, v_at
    FROM (SELECT cc_code FROM ref.mapping_at(v_date)
          GROUP BY cc_code HAVING COUNT(*) <> 1 OR BOOL_OR(bu_code IS NULL)) x;

    -- R08 no duplicate active postings
    INSERT INTO fin.reconciliations
    SELECT p_run_id, v_period, 'R08', 'Duplicate active postings', 'ALL',
           0, COUNT(*), -COUNT(*), CASE WHEN COUNT(*) = 0 THEN 'Pass' ELSE 'Fail' END, v_at
    FROM (SELECT idempotency_key FROM fin.journals WHERE status = 'Posted'
          GROUP BY idempotency_key HAVING COUNT(*) > 1) x;

    SELECT COUNT(*) INTO v_fails FROM fin.reconciliations WHERE run_id = p_run_id AND result = 'Fail';
    RETURN v_fails;
END;
$$;
