-- Task engine: dependencies, leases, retries, delegation depth, engine status.

CREATE TABLE task_dependency (
    task_id    TEXT NOT NULL REFERENCES task (task_id),
    depends_on TEXT NOT NULL REFERENCES task (task_id),
    PRIMARY KEY (task_id, depends_on),
    CHECK (task_id <> depends_on)
);
CREATE INDEX idx_task_dependency_target ON task_dependency (depends_on);

ALTER TABLE task ADD COLUMN kind TEXT NOT NULL DEFAULT 'work' CHECK (kind IN ('work', 'delegation'));
ALTER TABLE task ADD COLUMN depth INTEGER NOT NULL DEFAULT 0 CHECK (depth BETWEEN 0 AND 3);
ALTER TABLE task ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE task ADD COLUMN recoveries INTEGER NOT NULL DEFAULT 0;
ALTER TABLE task ADD COLUMN not_before TEXT;
ALTER TABLE task ADD COLUMN lease_owner TEXT;
ALTER TABLE task ADD COLUMN lease_expires_at TEXT;
ALTER TABLE task ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0;
ALTER TABLE task ADD COLUMN result_summary TEXT;
CREATE INDEX idx_task_lease ON task (status, lease_expires_at);

CREATE TABLE engine_status (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    instance     TEXT NOT NULL,
    pid          INTEGER NOT NULL,
    started_at   TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    cap          INTEGER NOT NULL,
    running      INTEGER NOT NULL,
    mode         TEXT NOT NULL
);
