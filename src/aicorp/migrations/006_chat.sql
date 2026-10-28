-- Two-way chat with the Advisor: outbound queue state, de-duplication of inbound messages.

ALTER TABLE chat_message ADD COLUMN state       TEXT;      -- outbound only: queued | sending | sent | failed
ALTER TABLE chat_message ADD COLUMN external_id TEXT;      -- provider message id (inbound de-duplication)
ALTER TABLE chat_message ADD COLUMN attempts    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chat_message ADD COLUMN next_try_at TEXT;
ALTER TABLE chat_message ADD COLUMN sent_at     TEXT;
ALTER TABLE chat_message ADD COLUMN error       TEXT;
ALTER TABLE chat_message ADD COLUMN reply_to    INTEGER REFERENCES chat_message (msg_id);

CREATE UNIQUE INDEX idx_chat_external ON chat_message (channel, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX idx_chat_queue ON chat_message (state, next_try_at);
CREATE INDEX idx_chat_ts ON chat_message (ts);
