-- Advisor: decisions, reports, nag bookkeeping, per-notification channel override.

CREATE TABLE decision (
    decision_id    TEXT PRIMARY KEY,
    project_id     TEXT REFERENCES project (project_id),
    meeting_id     TEXT REFERENCES meeting (meeting_id),
    task_id        TEXT REFERENCES task (task_id),
    title          TEXT NOT NULL,
    text           TEXT,
    status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'followed_up', 'dropped')),
    follow_up_by   TEXT,
    created_at     TEXT NOT NULL,
    closed_at      TEXT,
    md_path        TEXT
);
CREATE INDEX idx_decision_open ON decision (status, follow_up_by);

CREATE TABLE report (
    report_id  TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    path       TEXT NOT NULL,
    period     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_report_created ON report (created_at);

-- nag engine bookkeeping and an optional explicit channel list (overrides severity routing)
ALTER TABLE notification ADD COLUMN channels TEXT;
ALTER TABLE notification ADD COLUMN last_nag_at TEXT;

ALTER TABLE project ADD COLUMN notes TEXT;
ALTER TABLE meeting ADD COLUMN round_task_id TEXT;       -- consolidation task of a bot meeting
