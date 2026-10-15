CREATE TABLE schema_meta (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  version INTEGER NOT NULL
);
INSERT INTO schema_meta(singleton, version) VALUES (1, 1);
CREATE TABLE state_meta (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  generation INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
INSERT INTO state_meta(singleton, generation, created_at) VALUES (1, 0, strftime('%Y-%m-%dT%H:%M:%fZ','now'));

CREATE TABLE operations (
  operation_id TEXT PRIMARY KEY,
  operation_type TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('COMMITTED'))
);

CREATE TABLE request_nonces (
  nonce TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL
);

CREATE TABLE ownership (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  cloud_quota_exclusive INTEGER NOT NULL DEFAULT 0 CHECK (cloud_quota_exclusive IN (0,1)),
  windows_sender_disabled INTEGER NOT NULL DEFAULT 0 CHECK (windows_sender_disabled IN (0,1)),
  cloud_delivery_enabled INTEGER NOT NULL DEFAULT 0 CHECK (cloud_delivery_enabled IN (0,1)),
  updated_at TEXT NOT NULL
);
INSERT INTO ownership(singleton, updated_at) VALUES (1, '1970-01-01T00:00:00.000Z');

CREATE TABLE quota_bucket (
  period TEXT NOT NULL CHECK (period IN ('day','minute')),
  bucket_key TEXT NOT NULL,
  used_credits INTEGER NOT NULL CHECK (used_credits >= 0),
  limit_credits INTEGER NOT NULL CHECK (limit_credits > 0),
  CHECK (used_credits <= limit_credits),
  PRIMARY KEY(period, bucket_key)
);
CREATE TABLE quota_reservation (
  reservation_id TEXT PRIMARY KEY,
  provider TEXT NOT NULL CHECK (provider IN ('twelve_data')),
  utc_day TEXT NOT NULL,
  utc_minute TEXT NOT NULL,
  credits INTEGER NOT NULL CHECK (credits BETWEEN 1 AND 8),
  created_at TEXT NOT NULL
);
CREATE INDEX quota_reservation_created ON quota_reservation(created_at);

CREATE TABLE run_claim (
  market_date TEXT PRIMARY KEY,
  run_id TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('CLAIMED','RUNNING','REPORT_READY','DELIVERY_IN_PROGRESS','COMPLETE','FAILED','PARTIAL_DELIVERY','DELIVERY_UNKNOWN')),
  claim_operation_id TEXT NOT NULL,
  last_operation_id TEXT NOT NULL,
  claimed_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  reason_code TEXT
);

CREATE TABLE delivery_recipient (
  market_date TEXT NOT NULL,
  recipient_hmac TEXT NOT NULL CHECK (length(recipient_hmac) = 72 AND substr(recipient_hmac,1,8) = 'hmac256:'),
  status TEXT NOT NULL CHECK (status IN ('SENDING','SENT','UNKNOWN','FAILED_SAFE')),
  attempt INTEGER NOT NULL CHECK (attempt > 0),
  attempt_id TEXT NOT NULL,
  last_operation_id TEXT NOT NULL,
  send_started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  safe_error TEXT,
  reviewer_hmac TEXT,
  reconciliation_code TEXT,
  PRIMARY KEY(market_date, recipient_hmac),
  FOREIGN KEY(market_date) REFERENCES run_claim(market_date)
);
CREATE INDEX delivery_recipient_updated ON delivery_recipient(updated_at);

CREATE TABLE audit_event (
  audit_id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  subject_ref TEXT NOT NULL,
  actor_ref TEXT NOT NULL,
  created_at TEXT NOT NULL
);
