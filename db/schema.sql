-- CloudOps Sentinel Schema
-- Mirrors what you'd find in a real SRE platform's operational DB

-- ── Nodes ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS nodes (
    id          SERIAL PRIMARY KEY,
    hostname    VARCHAR(255) UNIQUE NOT NULL,
    role        VARCHAR(50) NOT NULL,          -- web, app, db, cache
    environment VARCHAR(50) NOT NULL DEFAULT 'production',
    ip_address  VARCHAR(45),
    registered_at TIMESTAMPTZ DEFAULT NOW(),
    last_seen   TIMESTAMPTZ,
    active      BOOLEAN DEFAULT TRUE
);

-- ── Health Snapshots ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS health_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    node_id         INT REFERENCES nodes(id) ON DELETE CASCADE,
    collected_at    TIMESTAMPTZ DEFAULT NOW(),
    cpu_percent     NUMERIC(5,2),
    memory_percent  NUMERIC(5,2),
    disk_percent    NUMERIC(5,2),
    load_avg_1m     NUMERIC(6,3),
    load_avg_5m     NUMERIC(6,3),
    load_avg_15m    NUMERIC(6,3),
    running_services JSONB DEFAULT '[]',
    stopped_services JSONB DEFAULT '[]',
    open_ports      JSONB DEFAULT '[]',
    raw_metrics     JSONB DEFAULT '{}'
);

CREATE INDEX idx_health_node_time ON health_snapshots(node_id, collected_at DESC);

-- ── Alerts ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS alerts (
    id              BIGSERIAL PRIMARY KEY,
    node_id         INT REFERENCES nodes(id) ON DELETE CASCADE,
    alert_type      VARCHAR(100) NOT NULL,     -- HIGH_DISK, SERVICE_DOWN, HIGH_CPU
    severity        VARCHAR(20) NOT NULL,       -- critical, warning, info
    message         TEXT NOT NULL,
    details         JSONB DEFAULT '{}',
    triggered_at    TIMESTAMPTZ DEFAULT NOW(),
    resolved_at     TIMESTAMPTZ,
    resolved        BOOLEAN DEFAULT FALSE
);

CREATE INDEX idx_alerts_node ON alerts(node_id, triggered_at DESC);
CREATE INDEX idx_alerts_unresolved ON alerts(resolved) WHERE resolved = FALSE;

-- ── Remediation Actions ──────────────────────────────────────────
-- ITIL-style change record for every automated action
CREATE TABLE IF NOT EXISTS remediation_actions (
    id              BIGSERIAL PRIMARY KEY,
    alert_id        BIGINT REFERENCES alerts(id),
    node_id         INT REFERENCES nodes(id),
    action_type     VARCHAR(100) NOT NULL,     -- LOG_ROTATION, SERVICE_RESTART, etc
    playbook        VARCHAR(255),
    status          VARCHAR(20) DEFAULT 'pending', -- pending, running, success, failed, rolled_back
    initiated_by    VARCHAR(100) DEFAULT 'sentinel-automation',
    change_record   VARCHAR(100),              -- CHG-YYYYMMDD-XXXXX
    started_at      TIMESTAMPTZ DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    output          TEXT,
    error           TEXT
);

-- ── Config Drift Reports ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS drift_reports (
    id                  BIGSERIAL PRIMARY KEY,
    node_id             INT REFERENCES nodes(id),
    baseline_name       VARCHAR(255) NOT NULL,
    detected_at         TIMESTAMPTZ DEFAULT NOW(),
    missing_packages    JSONB DEFAULT '[]',
    stopped_services    JSONB DEFAULT '[]',
    sysctl_drift        JSONB DEFAULT '{}',
    package_version_drift JSONB DEFAULT '{}',
    drift_score         INT DEFAULT 0,        -- number of drifted items
    remediated          BOOLEAN DEFAULT FALSE
);

-- ── AI Diagnoses ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ai_diagnoses (
    id              BIGSERIAL PRIMARY KEY,
    alert_id        BIGINT REFERENCES alerts(id),
    node_id         INT REFERENCES nodes(id),
    model           VARCHAR(100),
    root_cause      TEXT,
    confidence      NUMERIC(4,3),             -- 0.000 to 1.000
    recommended_actions JSONB DEFAULT '[]',
    escalate_to_human   BOOLEAN DEFAULT FALSE,
    raw_response    TEXT,
    diagnosed_at    TIMESTAMPTZ DEFAULT NOW(),
    tokens_used     INT
);

-- ── Patch Jobs ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS patch_jobs (
    id              BIGSERIAL PRIMARY KEY,
    job_name        VARCHAR(255) NOT NULL,
    patch_type      VARCHAR(50) DEFAULT 'security', -- security, full, kernel
    target_nodes    JSONB NOT NULL DEFAULT '[]',
    canary_nodes    JSONB NOT NULL DEFAULT '[]',
    batch_size      INT DEFAULT 10,
    status          VARCHAR(20) DEFAULT 'pending',
    current_batch   INT DEFAULT 0,
    nodes_patched   JSONB DEFAULT '[]',
    nodes_failed    JSONB DEFAULT '[]',
    rollback_triggered BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    created_by      VARCHAR(100) DEFAULT 'sentinel'
);

-- ── Seed initial nodes ───────────────────────────────────────────
INSERT INTO nodes (hostname, role, environment, ip_address) VALUES
    ('node-01', 'web',   'production', '172.20.0.11'),
    ('node-02', 'web',   'production', '172.20.0.12'),
    ('node-03', 'app',   'production', '172.20.0.13'),
    ('node-04', 'db',    'production', '172.20.0.14'),
    ('node-05', 'cache', 'production', '172.20.0.15')
ON CONFLICT (hostname) DO NOTHING;
