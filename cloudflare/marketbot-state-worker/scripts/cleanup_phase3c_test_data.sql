-- TEST-only cleanup template. Execute only via seed_phase3c_test_identity.ps1 -Cleanup,
-- which verifies wrangler.test.jsonc and fills the admin-secret hash placeholder.
-- This script never updates the ownership table and leaves request_nonces to expire.
-- Every mutation is deliberately guarded by the TEST identity attestation. A hash
-- mismatch is valid SQL that affects zero rows; the PowerShell helper must verify
-- the identity/hash before submitting this file and validate residue afterward.
DELETE FROM delivery_recipient
WHERE market_date BETWEEN '2090-01-01' AND '2099-12-31'
  AND recipient_hmac IN (
    'hmac256:ac00581af76fae2942fefc401985c751c927c4412b3e8486f54cda8e4d420419',
    'hmac256:ad4e4df2c02723b74f014769278cd2c1edac43e7f17751f1b9048f40b11aa692'
  )
  AND EXISTS (SELECT 1 FROM test_environment_identity WHERE singleton=1 AND database_id='8049a22c-eb1f-4e9d-928d-29b35dcc6f70' AND worker_name='marketbot-state-api-test' AND mode='phase3c-synthetic-mutations-v1' AND attestation_hash='__TEST_ADMIN_SECRET_SHA256__');

DELETE FROM run_claim
WHERE market_date BETWEEN '2090-01-01' AND '2099-12-31' AND run_id LIKE '3c%'
  AND EXISTS (SELECT 1 FROM test_environment_identity WHERE singleton=1 AND database_id='8049a22c-eb1f-4e9d-928d-29b35dcc6f70' AND worker_name='marketbot-state-api-test' AND mode='phase3c-synthetic-mutations-v1' AND attestation_hash='__TEST_ADMIN_SECRET_SHA256__');

-- Subtract only synthetic reservation credits. Buckets can be shared by
-- synthetic and non-synthetic reservations, so never delete a whole bucket
-- solely because a 3c reservation referenced it.
UPDATE quota_bucket
SET used_credits = used_credits - (
  SELECT COALESCE(SUM(r.credits), 0) FROM quota_reservation r
  WHERE r.reservation_id LIKE '3c%' AND r.utc_minute=quota_bucket.bucket_key
)
WHERE period='minute'
  AND EXISTS (SELECT 1 FROM quota_reservation r WHERE r.reservation_id LIKE '3c%' AND r.utc_minute=quota_bucket.bucket_key)
  AND used_credits >= (SELECT COALESCE(SUM(r.credits), 0) FROM quota_reservation r WHERE r.reservation_id LIKE '3c%' AND r.utc_minute=quota_bucket.bucket_key)
  AND EXISTS (SELECT 1 FROM test_environment_identity WHERE singleton=1 AND database_id='8049a22c-eb1f-4e9d-928d-29b35dcc6f70' AND worker_name='marketbot-state-api-test' AND mode='phase3c-synthetic-mutations-v1' AND attestation_hash='__TEST_ADMIN_SECRET_SHA256__');

UPDATE quota_bucket
SET used_credits = used_credits - (
  SELECT COALESCE(SUM(r.credits), 0) FROM quota_reservation r
  WHERE r.reservation_id LIKE '3c%' AND r.utc_day=quota_bucket.bucket_key
)
WHERE period='day'
  AND EXISTS (SELECT 1 FROM quota_reservation r WHERE r.reservation_id LIKE '3c%' AND r.utc_day=quota_bucket.bucket_key)
  AND used_credits >= (SELECT COALESCE(SUM(r.credits), 0) FROM quota_reservation r WHERE r.reservation_id LIKE '3c%' AND r.utc_day=quota_bucket.bucket_key)
  AND EXISTS (SELECT 1 FROM test_environment_identity WHERE singleton=1 AND database_id='8049a22c-eb1f-4e9d-928d-29b35dcc6f70' AND worker_name='marketbot-state-api-test' AND mode='phase3c-synthetic-mutations-v1' AND attestation_hash='__TEST_ADMIN_SECRET_SHA256__');

-- Remove only zeroed buckets that were touched by a synthetic reservation and
-- have no non-synthetic reservation. Do this before deleting those reservations.
DELETE FROM quota_bucket
WHERE used_credits=0 AND (
  (period='minute'
   AND EXISTS (SELECT 1 FROM quota_reservation s WHERE s.reservation_id LIKE '3c%' AND s.utc_minute=quota_bucket.bucket_key)
   AND NOT EXISTS (SELECT 1 FROM quota_reservation r WHERE r.reservation_id NOT LIKE '3c%' AND r.utc_minute=quota_bucket.bucket_key))
  OR (period='day'
   AND EXISTS (SELECT 1 FROM quota_reservation s WHERE s.reservation_id LIKE '3c%' AND s.utc_day=quota_bucket.bucket_key)
   AND NOT EXISTS (SELECT 1 FROM quota_reservation r WHERE r.reservation_id NOT LIKE '3c%' AND r.utc_day=quota_bucket.bucket_key))
)
AND EXISTS (SELECT 1 FROM test_environment_identity WHERE singleton=1 AND database_id='8049a22c-eb1f-4e9d-928d-29b35dcc6f70' AND worker_name='marketbot-state-api-test' AND mode='phase3c-synthetic-mutations-v1' AND attestation_hash='__TEST_ADMIN_SECRET_SHA256__');

DELETE FROM quota_reservation WHERE reservation_id LIKE '3c%'
  AND EXISTS (SELECT 1 FROM test_environment_identity WHERE singleton=1 AND database_id='8049a22c-eb1f-4e9d-928d-29b35dcc6f70' AND worker_name='marketbot-state-api-test' AND mode='phase3c-synthetic-mutations-v1' AND attestation_hash='__TEST_ADMIN_SECRET_SHA256__');

DELETE FROM audit_event WHERE audit_id LIKE '3c%'
  AND EXISTS (SELECT 1 FROM test_environment_identity WHERE singleton=1 AND database_id='8049a22c-eb1f-4e9d-928d-29b35dcc6f70' AND worker_name='marketbot-state-api-test' AND mode='phase3c-synthetic-mutations-v1' AND attestation_hash='__TEST_ADMIN_SECRET_SHA256__');

DELETE FROM operations WHERE operation_id LIKE '3c%'
  AND EXISTS (SELECT 1 FROM test_environment_identity WHERE singleton=1 AND database_id='8049a22c-eb1f-4e9d-928d-29b35dcc6f70' AND worker_name='marketbot-state-api-test' AND mode='phase3c-synthetic-mutations-v1' AND attestation_hash='__TEST_ADMIN_SECRET_SHA256__');

DELETE FROM test_environment_identity WHERE singleton=1 AND database_id='8049a22c-eb1f-4e9d-928d-29b35dcc6f70' AND worker_name='marketbot-state-api-test' AND mode='phase3c-synthetic-mutations-v1' AND attestation_hash='__TEST_ADMIN_SECRET_SHA256__';
