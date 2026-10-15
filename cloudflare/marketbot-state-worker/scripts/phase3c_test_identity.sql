-- Apply ONLY to the isolated marketbot-test D1 using wrangler.test.jsonc.
-- This is a one-row runtime attestation, not a schema migration. The seed script
-- replaces the placeholder with SHA-256(TEST STATE_ADMIN_HMAC_SECRET). Never add
-- this file to migrations/ or execute it with wrangler.jsonc.
CREATE TABLE IF NOT EXISTS test_environment_identity (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  database_id TEXT NOT NULL,
  worker_name TEXT NOT NULL,
  mode TEXT NOT NULL,
  attestation_hash TEXT NOT NULL CHECK (length(attestation_hash) = 64)
);

INSERT OR IGNORE INTO test_environment_identity(singleton, database_id, worker_name, mode, attestation_hash)
VALUES (1, '8049a22c-eb1f-4e9d-928d-29b35dcc6f70', 'marketbot-state-api-test', 'phase3c-synthetic-mutations-v1', '__TEST_ADMIN_SECRET_SHA256__');
