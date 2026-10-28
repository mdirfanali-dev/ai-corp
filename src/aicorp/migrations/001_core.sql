-- AICorp core schema (migration 001)

CREATE TABLE employee (
    emp_id       TEXT PRIMARY KEY,
    name         TEXT NOT NULL COLLATE NOCASE UNIQUE,
    kind         TEXT NOT NULL CHECK (kind IN ('permanent', 'temporary', 'system')),
    status       TEXT NOT NULL DEFAULT 'idle'
                 CHECK (status IN ('idle', 'working', 'blocked', 'paused', 'archived')),
    reports_to   TEXT REFERENCES employee (emp_id),
    workspace    TEXT,
    config_path  TEXT,
    notes        TEXT,
    expires_at   TEXT,
    created_at   TEXT NOT NULL,
    deleted_at   TEXT,
    deleted_by   TEXT
);

CREATE TRIGGER employee_id_immutable
BEFORE UPDATE OF emp_id ON employee
BEGIN
    SELECT RAISE(ABORT, 'emp_id is immutable');
END;

CREATE TABLE role (
    role_id            TEXT PRIMARY KEY,
    name               TEXT NOT NULL COLLATE NOCASE UNIQUE,
    description        TEXT,
    permission_profile TEXT NOT NULL DEFAULT '{}',
    skills             TEXT NOT NULL DEFAULT '[]',
    created_at         TEXT NOT NULL
);

CREATE TABLE employee_role (
    emp_id      TEXT NOT NULL REFERENCES employee (emp_id),
    role_id     TEXT NOT NULL REFERENCES role (role_id),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY (emp_id, role_id)
);

CREATE TABLE team (
    team_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL COLLATE NOCASE UNIQUE,
    description TEXT,
    head_emp_id TEXT REFERENCES employee (emp_id),
    created_at  TEXT NOT NULL,
    deleted_at  TEXT
);

CREATE TABLE team_member (
    team_id TEXT NOT NULL REFERENCES team (team_id),
    emp_id  TEXT NOT NULL REFERENCES employee (emp_id),
    PRIMARY KEY (team_id, emp_id)
);

CREATE TABLE project (
    project_id     TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    goal           TEXT,
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'paused', 'done')),
    owner_emp_id   TEXT REFERENCES employee (emp_id),
    next_review_at TEXT,
    created_at     TEXT NOT NULL,
    deleted_at     TEXT
);

CREATE TABLE meeting (
    meeting_id   TEXT PRIMARY KEY,
    project_id   TEXT REFERENCES project (project_id),
    title        TEXT NOT NULL,
    scheduled_at TEXT,
    agenda       TEXT,
    minutes_path TEXT,
    status       TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'held', 'cancelled')),
    created_at   TEXT NOT NULL,
    deleted_at   TEXT
);

CREATE TABLE meeting_attendee (
    meeting_id    TEXT NOT NULL REFERENCES meeting (meeting_id),
    attendee_id   TEXT NOT NULL,
    attendee_kind TEXT NOT NULL CHECK (attendee_kind IN ('employee', 'founder')),
    PRIMARY KEY (meeting_id, attendee_id)
);

CREATE TABLE task (
    task_id     TEXT PRIMARY KEY,
    parent_id   TEXT REFERENCES task (task_id),
    project_id  TEXT REFERENCES project (project_id),
    title       TEXT NOT NULL,
    description TEXT,
    owner_emp_id TEXT REFERENCES employee (emp_id),
    status      TEXT NOT NULL DEFAULT 'created'
                CHECK (status IN ('created', 'assigned', 'running', 'blocked', 'review',
                                  'awaiting_approval', 'done', 'rejected', 'failed')),
    priority    INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 5),
    risk_tier   INTEGER NOT NULL DEFAULT 1 CHECK (risk_tier BETWEEN 0 AND 4),
    due_at      TEXT,
    checkpoint  TEXT,
    created_by  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    deleted_at  TEXT
);
CREATE INDEX idx_task_status ON task (status);
CREATE INDEX idx_task_owner ON task (owner_emp_id);
CREATE INDEX idx_task_due ON task (due_at);

CREATE TABLE action_item (
    item_id     TEXT PRIMARY KEY,
    meeting_id  TEXT NOT NULL REFERENCES meeting (meeting_id),
    task_id     TEXT REFERENCES task (task_id),
    description TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- Append-only audit log; every row is chained to the previous one by hash.
CREATE TABLE event (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TEXT NOT NULL,
    task_id           TEXT,
    actor_type        TEXT NOT NULL CHECK (actor_type IN ('founder', 'employee', 'system')),
    actor_id          TEXT,
    actor_name        TEXT NOT NULL,
    actor_designation TEXT,
    acting_as_emp_id  TEXT,
    acting_as_name    TEXT,
    acting_as_role    TEXT,
    mode              TEXT NOT NULL DEFAULT 'normal'
                      CHECK (mode IN ('normal', 'impersonate', 'interrupt', 'chat')),
    type              TEXT NOT NULL,
    summary           TEXT NOT NULL,
    payload           TEXT NOT NULL DEFAULT '{}',
    prev_hash         TEXT NOT NULL,
    hash              TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_event_task ON event (task_id);
CREATE INDEX idx_event_ts ON event (ts);
CREATE INDEX idx_event_actor ON event (actor_name);

CREATE TRIGGER event_no_update BEFORE UPDATE ON event
BEGIN
    SELECT RAISE(ABORT, 'event log is append-only');
END;

CREATE TRIGGER event_no_delete BEFORE DELETE ON event
BEGIN
    SELECT RAISE(ABORT, 'event log is append-only');
END;

-- Full-text search over the audit log.
CREATE VIRTUAL TABLE event_fts USING fts5 (
    summary, payload, actor_name, acting_as_name, type,
    content = 'event', content_rowid = 'id'
);

CREATE TRIGGER event_fts_insert AFTER INSERT ON event
BEGIN
    INSERT INTO event_fts (rowid, summary, payload, actor_name, acting_as_name, type)
    VALUES (new.id, new.summary, new.payload, new.actor_name, new.acting_as_name, new.type);
END;

CREATE TABLE block (
    block_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL REFERENCES task (task_id),
    reason      TEXT NOT NULL CHECK (reason IN (
                    'USAGE_LIMIT', 'CAPTCHA', 'LOGIN_REQUIRED', 'UNKNOWN_UI', 'RUNNER_ERROR',
                    'PERMISSION_DENIED', 'NEEDS_APPROVAL', 'NEEDS_INPUT', 'TIMEOUT',
                    'RAM_PRESSURE', 'DISK_LOW', 'SYSTEM_ERROR')),
    detail      TEXT,
    since       TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT
);

CREATE TABLE approval (
    approval_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL REFERENCES task (task_id),
    tier         INTEGER NOT NULL CHECK (tier BETWEEN 0 AND 4),
    requested_at TEXT NOT NULL,
    decided_at   TEXT,
    decision     TEXT CHECK (decision IN ('approved', 'rejected', 'revise')),
    note         TEXT
);

CREATE TABLE notification (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    category         TEXT NOT NULL,
    severity         TEXT NOT NULL CHECK (severity IN ('info', 'warn', 'critical')),
    title            TEXT NOT NULL,
    detail           TEXT,
    entity_type      TEXT,
    entity_id        TEXT,
    created_at       TEXT NOT NULL,
    read_at          TEXT,
    ack_at           TEXT,
    snooze_until     TEXT,
    pinned           INTEGER NOT NULL DEFAULT 0,
    escalation_level INTEGER NOT NULL DEFAULT 0,
    dedupe_key       TEXT
);
CREATE INDEX idx_notification_unread ON notification (read_at);
CREATE INDEX idx_notification_dedupe ON notification (dedupe_key);

CREATE TABLE notification_delivery (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id INTEGER NOT NULL REFERENCES notification (id),
    channel         TEXT NOT NULL CHECK (channel IN ('dashboard', 'telegram', 'email')),
    sent_at         TEXT,
    status          TEXT NOT NULL
);

CREATE TABLE schedule (
    job      TEXT PRIMARY KEY,
    spec     TEXT NOT NULL,
    enabled  INTEGER NOT NULL DEFAULT 1,
    last_run TEXT,
    next_run TEXT
);

CREATE TABLE job_run (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    catch_up    INTEGER NOT NULL DEFAULT 0,
    detail      TEXT
);

CREATE TABLE chat_message (
    msg_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT NOT NULL CHECK (channel IN ('telegram', 'email')),
    direction  TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    sender     TEXT,
    text       TEXT NOT NULL,
    ts         TEXT NOT NULL,
    handled_as TEXT,
    task_id    TEXT REFERENCES task (task_id)
);

CREATE TABLE usage (
    usage_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    emp_id     TEXT REFERENCES employee (emp_id),
    model      TEXT,
    runs       INTEGER NOT NULL DEFAULT 1,
    est_tokens INTEGER
);

CREATE TABLE artifact (
    artifact_id TEXT PRIMARY KEY,
    task_id     TEXT REFERENCES task (task_id),
    path        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
