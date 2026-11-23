-- =====================================================================
-- 04  Expense policy governance: policy, claims, cases, governance controls
-- Schema pol, in the same chargeback database. Rules: docs/expense_policy.md
-- =====================================================================

DROP SCHEMA IF EXISTS pol CASCADE;
CREATE SCHEMA pol;

-- ---------------------------------------------------------------- policy (reference)
CREATE TABLE pol.employees (
    employee_id          TEXT PRIMARY KEY,
    cc_code              TEXT NOT NULL,
    dept_code            TEXT NOT NULL,
    bu_code              TEXT NOT NULL,
    role                 TEXT NOT NULL CHECK (role IN ('Staff', 'Manager', 'Department head', 'Finance director')),
    line_manager_id      TEXT REFERENCES pol.employees,
    dept_head_id         TEXT REFERENCES pol.employees,
    finance_director_id  TEXT REFERENCES pol.employees
);

CREATE TABLE pol.policy_rules (
    version     INT     NOT NULL,
    valid_from  DATE    NOT NULL,
    valid_to    DATE    NOT NULL,
    rule_key    TEXT    NOT NULL,
    value       NUMERIC NOT NULL,
    PRIMARY KEY (version, rule_key)
);

CREATE TABLE pol.high_cost_cities (
    city  TEXT PRIMARY KEY
);

CREATE TABLE pol.approval_matrix (
    approval_level  INT PRIMARY KEY,           -- 1 line manager, 2 department head, 3 finance director
    min_total       NUMERIC(18,2) NOT NULL,
    max_total       NUMERIC(18,2) NOT NULL,
    approver_role   TEXT NOT NULL
);

CREATE TABLE pol.check_catalogue (
    check_code  TEXT PRIMARY KEY,
    check_name  TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('High', 'Medium', 'Low'))
);
INSERT INTO pol.check_catalogue VALUES
    ('P01', 'Receipt missing for an item of $25 or more', 'Low'),
    ('P02', 'Hotel over the nightly cap for the city', 'Medium'),
    ('P03', 'Meals over the daily cap', 'Low'),
    ('P04', 'Business class or upgrade without pre-approval', 'Medium'),
    ('P05', 'Client entertainment over the limit or without attendees', 'Medium'),
    ('P06', 'Duplicate claim', 'High'),
    ('P07', 'Split claim to stay under an approval level', 'High'),
    ('P08', 'Approved at the wrong level or by the claimant', 'High'),
    ('P09', 'Submitted after the deadline', 'Low'),
    ('P10', 'Item not reimbursable', 'High');

CREATE TABLE pol.sla (
    severity             TEXT PRIMARY KEY,
    decision_wd          INT NOT NULL,        -- working days from intake to decision
    remediation_wd       INT NOT NULL,        -- working days from decision to recovery
    escalate_at_intake   NUMERIC(18,2)        -- escalate immediately above this amount (High only)
);
INSERT INTO pol.sla VALUES ('High', 3, 20, 1000), ('Medium', 5, 20, NULL), ('Low', 10, 20, NULL);

CREATE TABLE pol.exception_register (
    exception_id  TEXT PRIMARY KEY,
    employee_id   TEXT NOT NULL REFERENCES pol.employees,
    rule_code     TEXT NOT NULL REFERENCES pol.check_catalogue,
    reason        TEXT NOT NULL,
    approved_by   TEXT NOT NULL REFERENCES pol.employees,
    valid_from    DATE,
    valid_to      DATE,
    source        TEXT NOT NULL CHECK (source IN ('Pre-approved', 'Review decision')),
    case_id       INT,
    recorded_at   TIMESTAMP
);

-- ---------------------------------------------------------------- claims (as paid)
CREATE TABLE pol.claims (
    claim_id          TEXT PRIMARY KEY,
    employee_id       TEXT NOT NULL REFERENCES pol.employees,
    submitted_date    DATE NOT NULL,
    approved_by       TEXT NOT NULL REFERENCES pol.employees,
    approved_date     DATE NOT NULL,
    paid_date         DATE NOT NULL,
    claim_total       NUMERIC(18,2) NOT NULL,
    business_purpose  TEXT
);

CREATE TABLE pol.claim_lines (
    line_id           TEXT PRIMARY KEY,
    claim_id          TEXT NOT NULL REFERENCES pol.claims,
    expense_date      DATE NOT NULL,
    category          TEXT NOT NULL,
    merchant          TEXT NOT NULL,
    city              TEXT,
    nights            INT,
    attendees         INT,
    amount            NUMERIC(18,2) NOT NULL,
    receipt_id        TEXT,
    travel_class      TEXT,
    alcohol           BOOLEAN NOT NULL DEFAULT FALSE,
    client_present    BOOLEAN NOT NULL DEFAULT FALSE,
    pre_approval_ref  TEXT
);

-- What the (simulated) reviewer does on each case. Input to the simulation, like the source files.
CREATE TABLE pol.reviewer_actions (
    claim_id          TEXT NOT NULL REFERENCES pol.claims,
    check_code        TEXT NOT NULL REFERENCES pol.check_catalogue,
    decision          TEXT NOT NULL,
    disallowed        NUMERIC(18,2) NOT NULL,
    triage_after_wd   INT NOT NULL,
    decide_after_wd   INT NOT NULL,
    evidence          TEXT NOT NULL,
    recover_after_wd  INT,
    recovery_result   TEXT,
    retry_after_wd    INT,
    reviewer_note     TEXT,
    PRIMARY KEY (claim_id, check_code)
);

-- ---------------------------------------------------------------- review runs, cases, controls
CREATE TABLE pol.review_runs (
    run_id          SERIAL PRIMARY KEY,
    period          TEXT      NOT NULL,       -- month the claims were paid
    run_at          TIMESTAMP NOT NULL,
    claims_checked  INT       NOT NULL DEFAULT 0
);

CREATE TABLE pol.check_results (
    run_id              INT  NOT NULL REFERENCES pol.review_runs,
    claim_id            TEXT NOT NULL REFERENCES pol.claims,
    check_code          TEXT NOT NULL REFERENCES pol.check_catalogue,
    amount_in_question  NUMERIC(18,2) NOT NULL,
    policy_version      INT,
    detail              TEXT NOT NULL,
    PRIMARY KEY (run_id, claim_id, check_code)
);

CREATE TABLE pol.cases (
    case_id             SERIAL PRIMARY KEY,
    claim_id            TEXT NOT NULL REFERENCES pol.claims,
    check_code          TEXT NOT NULL REFERENCES pol.check_catalogue,
    employee_id         TEXT NOT NULL REFERENCES pol.employees,
    period              TEXT NOT NULL,
    severity            TEXT NOT NULL,
    amount_in_question  NUMERIC(18,2) NOT NULL,
    detail              TEXT NOT NULL,
    owner_role          TEXT NOT NULL DEFAULT 'Expense Policy Analyst',
    status              TEXT NOT NULL CHECK (status IN ('Open', 'Triaged', 'Decided', 'Remediation', 'Closed', 'Reopened')),
    opened_run_id       INT  NOT NULL REFERENCES pol.review_runs,
    opened_at           TIMESTAMP NOT NULL,
    triaged_at          TIMESTAMP,
    decision_due_at     TIMESTAMP NOT NULL,
    decision            TEXT CHECK (decision IN ('No breach', 'Exception approved', 'Partly reimbursed', 'Rejected', 'Corrected')),
    decided_at          TIMESTAMP,
    disallowed          NUMERIC(18,2) NOT NULL DEFAULT 0,
    evidence            TEXT,
    remediation_due_at  TIMESTAMP,
    recovered           NUMERIC(18,2) NOT NULL DEFAULT 0,
    closed_at           TIMESTAMP,
    escalated           BOOLEAN NOT NULL DEFAULT FALSE,
    UNIQUE (claim_id, check_code)
);

CREATE TABLE pol.case_events (
    case_id     INT       NOT NULL REFERENCES pol.cases,
    event_at    TIMESTAMP NOT NULL,
    event_type  TEXT      NOT NULL,
    detail      TEXT
);

CREATE TABLE pol.repeat_reports (
    employee_id  TEXT      NOT NULL REFERENCES pol.employees,
    reported_at  TIMESTAMP NOT NULL,
    case_count   INT       NOT NULL,
    reported_to  TEXT      NOT NULL REFERENCES pol.employees,
    PRIMARY KEY (employee_id, reported_at)
);

CREATE TABLE pol.governance_results (
    period        TEXT      NOT NULL,
    control_id    TEXT      NOT NULL,
    control_name  TEXT      NOT NULL,
    result        TEXT      NOT NULL CHECK (result IN ('Pass', 'Fail')),
    evidence_ref  TEXT      NOT NULL,
    detail        TEXT,
    prepared_by   TEXT      NOT NULL DEFAULT 'pipeline',
    reviewer_status TEXT    NOT NULL DEFAULT 'Reviewed (simulated)',
    recorded_at   TIMESTAMP NOT NULL,
    PRIMARY KEY (period, control_id)
);
