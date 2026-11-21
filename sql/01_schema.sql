-- =====================================================================
-- 01  Schema: reference data, source files, operations, finance, reporting
-- Database: chargeback
-- =====================================================================

DROP SCHEMA IF EXISTS rpt CASCADE;
DROP SCHEMA IF EXISTS fin CASCADE;
DROP SCHEMA IF EXISTS ops CASCADE;
DROP SCHEMA IF EXISTS src CASCADE;
DROP SCHEMA IF EXISTS ref CASCADE;

CREATE SCHEMA ref;   -- master data (hierarchy, pools, accounts, policies, calendar)
CREATE SCHEMA src;   -- source files exactly as received, with their manifest
CREATE SCHEMA ops;   -- runs, exceptions, escalations, controls
CREATE SCHEMA fin;   -- allocations, journals, simulated ledger, reconciliations
CREATE SCHEMA rpt;   -- reporting views for Excel and Power BI

-- ---------------------------------------------------------------- reference
CREATE TABLE ref.business_units (
    bu_code  TEXT PRIMARY KEY,
    bu_name  TEXT NOT NULL
);

-- Department -> business unit, effective-dated (a department can move unit)
CREATE TABLE ref.departments (
    dept_code   TEXT NOT NULL,
    dept_name   TEXT NOT NULL,
    bu_code     TEXT NOT NULL REFERENCES ref.business_units,
    valid_from  DATE NOT NULL,
    valid_to    DATE NOT NULL DEFAULT '9999-12-31',
    change_ref  TEXT,
    PRIMARY KEY (dept_code, valid_from),
    CHECK (valid_to >= valid_from)
);

-- Cost centre -> department, effective-dated (moves, openings, closures, renames)
CREATE TABLE ref.cost_centres (
    cc_code      TEXT    NOT NULL,
    cc_name      TEXT    NOT NULL,
    dept_code    TEXT    NOT NULL,
    remote_only  BOOLEAN NOT NULL DEFAULT FALSE,
    is_active    BOOLEAN NOT NULL DEFAULT TRUE,
    valid_from   DATE    NOT NULL,
    valid_to     DATE    NOT NULL DEFAULT '9999-12-31',
    change_ref   TEXT,
    PRIMARY KEY (cc_code, valid_from),
    CHECK (valid_to >= valid_from)
);

CREATE TABLE ref.gl_accounts (
    account       TEXT PRIMARY KEY,
    account_name  TEXT NOT NULL,
    account_type  TEXT NOT NULL
);

CREATE TABLE ref.cost_pools (
    pool_code          TEXT PRIMARY KEY,
    pool_name          TEXT NOT NULL,
    pool_cc            TEXT NOT NULL,
    source_account     TEXT NOT NULL REFERENCES ref.gl_accounts,
    allocated_account  TEXT NOT NULL REFERENCES ref.gl_accounts,
    recovery_account   TEXT NOT NULL REFERENCES ref.gl_accounts
);

-- Allocation policy per pool, effective-dated and versioned separately from the hierarchy
CREATE TABLE ref.allocation_policies (
    pool_code             TEXT    NOT NULL REFERENCES ref.cost_pools,
    version               INT     NOT NULL,
    driver                TEXT    NOT NULL,
    excludes_remote_only  BOOLEAN NOT NULL,
    valid_from            DATE    NOT NULL,
    valid_to              DATE    NOT NULL,
    PRIMARY KEY (pool_code, version)
);

CREATE TABLE ref.holidays (
    holiday_date  DATE PRIMARY KEY,
    holiday_name  TEXT NOT NULL
);

CREATE TABLE ref.periods (
    period        TEXT PRIMARY KEY,           -- 'YYYY-MM'
    period_start  DATE NOT NULL,
    period_end    DATE NOT NULL,
    status        TEXT NOT NULL DEFAULT 'Open' CHECK (status IN ('Open', 'Closed')),
    closed_at     TIMESTAMP
);

-- ---------------------------------------------------------------- source files
CREATE TABLE src.files (
    file_id        SERIAL PRIMARY KEY,
    period         TEXT      NOT NULL REFERENCES ref.periods,
    file_name      TEXT      NOT NULL,
    pool_code      TEXT,
    file_type      TEXT      NOT NULL CHECK (file_type IN ('EXPENSE', 'VOLUME', 'HIERARCHY')),
    version        INT       NOT NULL,       -- 1 = original, 2 = resubmission, 3 = post-close correction
    row_count      INT       NOT NULL,       -- as stated by the sender in the manifest
    control_total  NUMERIC(18,2),            -- as stated by the sender in the manifest
    received_at    TIMESTAMP NOT NULL,
    UNIQUE (period, file_name)
);

CREATE TABLE src.expense_lines (
    file_id     INT           NOT NULL REFERENCES src.files,
    line_no     INT           NOT NULL,
    period      TEXT          NOT NULL,
    pool_code   TEXT          NOT NULL,
    pool_cc     TEXT          NOT NULL,
    account     TEXT          NOT NULL,
    source_ref  TEXT          NOT NULL,
    amount      NUMERIC(18,2) NOT NULL,
    PRIMARY KEY (file_id, line_no)
);

CREATE TABLE src.volume_lines (
    file_id    INT     NOT NULL REFERENCES src.files,
    line_no    INT     NOT NULL,
    period     TEXT    NOT NULL,
    pool_code  TEXT    NOT NULL,
    cc_code    TEXT    NOT NULL,
    driver     TEXT    NOT NULL,
    quantity   NUMERIC NOT NULL,
    PRIMARY KEY (file_id, line_no)
);

CREATE TABLE src.hierarchy_changes (
    file_id         INT  NOT NULL REFERENCES src.files,
    line_no         INT  NOT NULL,
    request_id      TEXT NOT NULL,
    change_type     TEXT NOT NULL,
    object_code     TEXT NOT NULL,
    new_dept        TEXT,
    new_bu          TEXT,
    new_name        TEXT,
    effective_date  DATE NOT NULL,
    PRIMARY KEY (file_id, line_no)
);

-- ---------------------------------------------------------------- operations
CREATE TABLE ops.runs (
    run_id       SERIAL PRIMARY KEY,
    period       TEXT      NOT NULL REFERENCES ref.periods,
    run_type     TEXT      NOT NULL CHECK (run_type IN ('Cut-off', 'Rerun', 'Close', 'Adjustment')),
    run_at       TIMESTAMP NOT NULL,             -- simulated business time of the run
    posting_period TEXT    REFERENCES ref.periods,
    note         TEXT
);

-- Files used by each run (the latest version of each file received by run time)
CREATE TABLE ops.run_files (
    run_id   INT NOT NULL REFERENCES ops.runs,
    file_id  INT NOT NULL REFERENCES src.files,
    PRIMARY KEY (run_id, file_id)
);

-- Status of each pool in each run
CREATE TABLE ops.run_pools (
    run_id       INT  NOT NULL REFERENCES ops.runs,
    pool_code    TEXT NOT NULL REFERENCES ref.cost_pools,
    pool_status  TEXT NOT NULL,   -- Blocked, Waiting for file, Allocated, Already posted
    pool_cost    NUMERIC(18,2),
    PRIMARY KEY (run_id, pool_code)
);

-- Every failed check found in a run (raw findings)
CREATE TABLE ops.check_results (
    run_id          INT  NOT NULL REFERENCES ops.runs,
    check_code      TEXT NOT NULL,
    pool_code       TEXT,
    cc_code         TEXT,
    detail          TEXT NOT NULL,
    amount_at_risk  NUMERIC(18,2),
    blocks_pool     BOOLEAN NOT NULL
);

CREATE TABLE ops.check_catalogue (
    check_code     TEXT PRIMARY KEY,
    check_name     TEXT NOT NULL,
    severity       TEXT NOT NULL CHECK (severity IN ('Critical', 'High', 'Medium', 'Low')),
    blocks_pool    BOOLEAN NOT NULL,
    owner_role     TEXT NOT NULL,
    root_cause     TEXT NOT NULL
);

INSERT INTO ops.check_catalogue VALUES
 ('V01', 'Expense file control total does not match loaded total', 'Critical', TRUE,  'Shared Services Finance',  'RC01 Source data error'),
 ('V02', 'Volume for an unknown cost centre',                      'High',     TRUE,  'Cost Centre Data Owner',   'RC01 Source data error'),
 ('V03', 'Volume missing for an active, eligible cost centre',     'High',     TRUE,  'Cost Centre Data Owner',   'RC01 Source data error'),
 ('V04', 'Negative volume',                                        'High',     TRUE,  'Cost Centre Data Owner',   'RC01 Source data error'),
 ('V05', 'Duplicate volume rows',                                  'Medium',   TRUE,  'Cost Centre Data Owner',   'RC04 Duplicate data'),
 ('V06', 'Volume for an inactive cost centre',                     'Medium',   TRUE,  'Cost Centre Data Owner',   'RC03 Hierarchy/mapping error'),
 ('V07', 'Cost centre mapped to an unknown department',            'Critical', TRUE,  'FP&A Reporting',           'RC03 Hierarchy/mapping error'),
 ('V08', 'File not received by the cut-off',                       'Low',      FALSE, 'Cost Centre Data Owner',   'RC02 Late submission'),
 ('V09', 'Zero total driver units for a pool',                     'High',     TRUE,  'Cost Centre Data Owner',   'RC01 Source data error'),
 ('V10', 'File row count does not match the manifest',             'High',     TRUE,  'Shared Services Finance',  'RC01 Source data error');

CREATE TABLE ops.sla (
    severity        TEXT PRIMARY KEY,
    working_days    INT  NOT NULL,
    escalate_value  NUMERIC(18,2)            -- escalate immediately above this amount at risk
);
INSERT INTO ops.sla VALUES ('Critical', 1, 50000), ('High', 3, NULL), ('Medium', 5, NULL), ('Low', 10, NULL);

-- One exception per distinct problem (period + check + pool + cost centre), tracked to closure
CREATE TABLE ops.exceptions (
    exception_id     SERIAL PRIMARY KEY,
    period           TEXT      NOT NULL,
    check_code       TEXT      NOT NULL REFERENCES ops.check_catalogue,
    pool_code        TEXT,
    cc_code          TEXT,
    severity         TEXT      NOT NULL,
    owner_role       TEXT      NOT NULL,
    root_cause       TEXT      NOT NULL,
    description      TEXT      NOT NULL,
    amount_at_risk   NUMERIC(18,2),
    status           TEXT      NOT NULL CHECK (status IN ('Open', 'Resolved', 'Closed', 'Reopened')),
    opened_run_id    INT       NOT NULL REFERENCES ops.runs,
    opened_at        TIMESTAMP NOT NULL,
    due_at           TIMESTAMP NOT NULL,
    last_seen_run_id INT       NOT NULL REFERENCES ops.runs,
    resolved_at      TIMESTAMP,
    resolution_note  TEXT,
    closed_at        TIMESTAMP,
    escalated        BOOLEAN   NOT NULL DEFAULT FALSE
);
CREATE UNIQUE INDEX ux_exception_open
    ON ops.exceptions (period, check_code, COALESCE(pool_code, ''), COALESCE(cc_code, ''))
    WHERE status IN ('Open', 'Reopened');

CREATE TABLE ops.exception_events (
    exception_id  INT       NOT NULL REFERENCES ops.exceptions,
    event_at      TIMESTAMP NOT NULL,
    run_id        INT       REFERENCES ops.runs,
    event_type    TEXT      NOT NULL,   -- Opened, Still failing, Escalated, Resolved, Reopened, Closed
    note          TEXT
);

-- Monthly control evidence: every result kept, including failed attempts
CREATE TABLE ops.control_results (
    run_id           INT       NOT NULL REFERENCES ops.runs,
    period           TEXT      NOT NULL,
    control_id       TEXT      NOT NULL,
    control_name     TEXT      NOT NULL,
    result           TEXT      NOT NULL,   -- Pass, Fail, Approved, Rejected
    evidence_ref     TEXT      NOT NULL,
    detail           TEXT,
    prepared_by      TEXT      NOT NULL DEFAULT 'pipeline',
    reviewer_status  TEXT      NOT NULL DEFAULT 'Reviewed (simulated)',
    recorded_at      TIMESTAMP NOT NULL,
    PRIMARY KEY (run_id, control_id)
);

-- ---------------------------------------------------------------- finance
CREATE TABLE fin.allocations (
    allocation_id     SERIAL PRIMARY KEY,
    run_id            INT           NOT NULL REFERENCES ops.runs,
    source_period     TEXT          NOT NULL,      -- the month the cost belongs to
    pool_code         TEXT          NOT NULL,
    policy_version    INT           NOT NULL,
    driver            TEXT          NOT NULL,
    allocation_version INT          NOT NULL,      -- 1 = original, 2 = replacement after a correction
    pool_cost         NUMERIC(18,2) NOT NULL,
    total_units       NUMERIC       NOT NULL,
    rate              NUMERIC(18,6) NOT NULL,      -- cost per driver unit
    input_hash        TEXT          NOT NULL,      -- fingerprint of the inputs, for idempotency
    created_at        TIMESTAMP     NOT NULL
);

CREATE TABLE fin.allocation_lines (
    allocation_id     INT           NOT NULL REFERENCES fin.allocations,
    cc_code           TEXT          NOT NULL,
    dept_code         TEXT          NOT NULL,
    bu_code           TEXT          NOT NULL,
    units             NUMERIC       NOT NULL,
    share             NUMERIC(12,10) NOT NULL,
    exact_amount      NUMERIC(24,10) NOT NULL,
    allocated_amount  NUMERIC(18,2) NOT NULL,      -- after largest-remainder rounding
    PRIMARY KEY (allocation_id, cc_code)
);

CREATE TABLE fin.journals (
    journal_id        TEXT          PRIMARY KEY,   -- e.g. JE-2025-03-P2-V1
    journal_type      TEXT          NOT NULL CHECK (journal_type IN ('Source', 'Allocation', 'Reversal', 'Replacement', 'Catch-up')),
    source_period     TEXT          NOT NULL,
    posting_period    TEXT          NOT NULL REFERENCES ref.periods,
    pool_code         TEXT,
    allocation_id     INT           REFERENCES fin.allocations,
    reverses_journal  TEXT          REFERENCES fin.journals,
    status            TEXT          NOT NULL CHECK (status IN ('Draft', 'Validated', 'Approved', 'Posted', 'Rejected')),
    idempotency_key   TEXT          NOT NULL,
    created_run_id    INT           NOT NULL REFERENCES ops.runs,
    created_at        TIMESTAMP     NOT NULL,
    validated_at      TIMESTAMP,
    approved_at       TIMESTAMP,
    approved_by       TEXT,
    posted_at         TIMESTAMP,
    rejection_reason  TEXT
);
-- No duplicate active posting for the same period, pool, type and version
CREATE UNIQUE INDEX ux_journal_idempotency ON fin.journals (idempotency_key) WHERE status <> 'Rejected';

CREATE TABLE fin.journal_lines (
    journal_id  TEXT          NOT NULL REFERENCES fin.journals,
    line_no     INT           NOT NULL,
    cc_code     TEXT          NOT NULL,
    account     TEXT          NOT NULL REFERENCES ref.gl_accounts,
    debit       NUMERIC(18,2) NOT NULL DEFAULT 0,
    credit      NUMERIC(18,2) NOT NULL DEFAULT 0,
    description TEXT,
    PRIMARY KEY (journal_id, line_no),
    CHECK (debit >= 0 AND credit >= 0 AND NOT (debit > 0 AND credit > 0))
);

-- Simulated general ledger: only posted journal lines
CREATE TABLE fin.ledger (
    ledger_id       SERIAL PRIMARY KEY,
    journal_id      TEXT          NOT NULL REFERENCES fin.journals,
    line_no         INT           NOT NULL,
    posting_period  TEXT          NOT NULL,
    source_period   TEXT          NOT NULL,
    cc_code         TEXT          NOT NULL,
    account         TEXT          NOT NULL,
    debit           NUMERIC(18,2) NOT NULL,
    credit          NUMERIC(18,2) NOT NULL,
    posted_at       TIMESTAMP     NOT NULL,
    UNIQUE (journal_id, line_no)
);

CREATE TABLE fin.reconciliations (
    run_id      INT           NOT NULL REFERENCES ops.runs,
    period      TEXT          NOT NULL,
    recon_id    TEXT          NOT NULL,
    recon_name  TEXT          NOT NULL,
    pool_code   TEXT          NOT NULL DEFAULT 'ALL',
    left_value  NUMERIC(18,2),
    right_value NUMERIC(18,2),
    difference  NUMERIC(18,2),
    result      TEXT          NOT NULL CHECK (result IN ('Pass', 'Fail')),
    recorded_at TIMESTAMP     NOT NULL,
    PRIMARY KEY (run_id, recon_id, pool_code)
);
