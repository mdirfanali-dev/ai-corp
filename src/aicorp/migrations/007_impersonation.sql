-- Impersonation sessions and interrupt/take-over records.

CREATE TABLE impersonation (
    imp_id         TEXT PRIMARY KEY,
    by_type        TEXT NOT NULL,                 -- founder | employee (the CEO)
    by_id          TEXT,
    by_name        TEXT NOT NULL,
    target_emp_id  TEXT NOT NULL REFERENCES employee (emp_id),
    target_name    TEXT NOT NULL,
    target_role    TEXT,
    override       INTEGER NOT NULL DEFAULT 0,    -- 1 = Founder override: not limited to the target's permissions
    reason         TEXT,
    started_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    ended_at       TEXT,
    ended_reason   TEXT
);
CREATE INDEX idx_imp_open ON impersonation (by_type, by_id, ended_at);

CREATE TABLE takeover (
    takeover_id  TEXT PRIMARY KEY,
    task_id      TEXT NOT NULL REFERENCES task (task_id),
    emp_id       TEXT NOT NULL REFERENCES employee (emp_id),
    status       TEXT NOT NULL CHECK (status IN ('requested', 'active', 'ended')),
    requested_at TEXT NOT NULL,
    started_at   TEXT,
    ended_at     TEXT,
    end_action   TEXT CHECK (end_action IN ('resume', 'complete', 'cancel', 'abandoned')),
    snapshot     TEXT
);
CREATE UNIQUE INDEX idx_takeover_open ON takeover (task_id) WHERE status <> 'ended';

CREATE TABLE takeover_note (
    note_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    takeover_id  TEXT NOT NULL REFERENCES takeover (takeover_id),
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('note', 'file')),
    text         TEXT NOT NULL
);

ALTER TABLE task ADD COLUMN interrupt_requested INTEGER NOT NULL DEFAULT 0;
