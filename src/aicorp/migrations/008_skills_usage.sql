-- Skill vetting and measured prompt sizes.

CREATE TABLE skill_vetting (
    name      TEXT PRIMARY KEY,
    status    TEXT NOT NULL CHECK (status IN ('vetted', 'rejected')),
    sha256    TEXT NOT NULL,                -- the exact content that was checked; a change makes the skill unvetted again
    vetted_at TEXT NOT NULL,
    vetted_by TEXT NOT NULL,
    checklist TEXT,
    note      TEXT
);

ALTER TABLE usage ADD COLUMN task_id TEXT;
ALTER TABLE usage ADD COLUMN prompt_chars INTEGER;     -- what was actually sent (task text, results, skills, rules)
ALTER TABLE usage ADD COLUMN baseline_chars INTEGER;   -- what a naive prompt for the same run would have been (see context.naive_size)
CREATE INDEX idx_usage_task ON usage (task_id);
CREATE INDEX idx_usage_ts ON usage (ts);
