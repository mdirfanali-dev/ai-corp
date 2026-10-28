-- Notification inbox, delivery bookkeeping, settings, usage-limit resume.

ALTER TABLE notification ADD COLUMN resolved_at TEXT;
ALTER TABLE notification ADD COLUMN repeat_count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE notification ADD COLUMN last_seen_at TEXT;
ALTER TABLE notification ADD COLUMN task_id TEXT;
ALTER TABLE notification ADD COLUMN link TEXT;
-- one open notification per cause: a second insert for the same cause is a duplicate, not a new alert
CREATE UNIQUE INDEX idx_notification_open_key ON notification (dedupe_key)
    WHERE resolved_at IS NULL AND dedupe_key IS NOT NULL;
CREATE INDEX idx_notification_created ON notification (created_at);
CREATE INDEX idx_notification_resolved ON notification (resolved_at);

-- delivery status: sending | sent | failed | suppressed
ALTER TABLE notification_delivery ADD COLUMN error TEXT;
ALTER TABLE notification_delivery ADD COLUMN attempted_at TEXT;
-- a channel is claimed at most once per notification (a failed attempt frees the claim for a retry)
CREATE UNIQUE INDEX idx_delivery_once ON notification_delivery (notification_id, channel)
    WHERE status IN ('sending', 'sent', 'suppressed');

ALTER TABLE block ADD COLUMN resume_at TEXT;

CREATE TABLE setting (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE notification_fts USING fts5 (
    title, detail, category,
    content = 'notification', content_rowid = 'id'
);

CREATE TRIGGER notification_fts_insert AFTER INSERT ON notification
BEGIN
    INSERT INTO notification_fts (rowid, title, detail, category)
    VALUES (new.id, new.title, new.detail, new.category);
END;

CREATE TRIGGER notification_fts_update AFTER UPDATE OF title, detail, category ON notification
BEGIN
    INSERT INTO notification_fts (notification_fts, rowid, title, detail, category)
    VALUES ('delete', old.id, old.title, old.detail, old.category);
    INSERT INTO notification_fts (rowid, title, detail, category)
    VALUES (new.id, new.title, new.detail, new.category);
END;
