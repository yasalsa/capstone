CREATE TABLE IF NOT EXISTS approved_senders (
  user_id TEXT NOT NULL,
  sender_email TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, sender_email)
);

CREATE TABLE IF NOT EXISTS cases (
  id BIGSERIAL PRIMARY KEY,
  intake_mode TEXT NOT NULL,
  provider_message_id TEXT NOT NULL,
  rfc822_message_id TEXT,
  user_id TEXT NOT NULL,
  sender_email TEXT NOT NULL,
  subject TEXT,
  received_at TEXT,
  raw_path TEXT NOT NULL,
  scan_result_json JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (intake_mode, provider_message_id)
);

CREATE TABLE IF NOT EXISTS quarantined_messages (
  id BIGSERIAL PRIMARY KEY,
  intake_mode TEXT NOT NULL,
  provider_message_id TEXT NOT NULL,
  rfc822_message_id TEXT,
  user_id TEXT,
  sender_email TEXT,
  subject TEXT,
  received_at TEXT,
  reason TEXT NOT NULL,
  raw_path TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (intake_mode, provider_message_id)
);
