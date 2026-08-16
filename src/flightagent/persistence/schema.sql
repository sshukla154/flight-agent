-- SQLite cache schema (T43, master plan S5 "Caching: SQLite, not DuckDB").
--
-- Two tables, deliberately split -- master plan S5: "The split means a
-- mapper bugfix or FX-source change re-normalizes from cached bytes with
-- NO new API calls -- worth the extra table on its own, and it gives you
-- `flightagent replay --run-id=...` for free." raw_payloads holds exactly
-- what the provider returned (keyed by raw_key, the request-shape hash);
-- normalized_offers holds the output of running a raw payload through the
-- normalizer + FX conversion (keyed by normalized_key, which folds in
-- raw_key + normalizer_version + fx_source_id -- see persistence/keys.py).
-- A change to either the normalizer or the FX source produces a NEW
-- normalized_key without touching raw_payloads at all, so the old raw
-- bytes are still there to re-normalize from.
--
-- Applied idempotently on every `db.connect()` call via
-- `CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS` -- safe to
-- run against an already-migrated database (including a fresh :memory:
-- database opened by a test), no separate migration-framework/versioning
-- needed at this project's scale (master plan S5: "a single schema.sql is
-- fine for this project's scale, no need for a migration framework").

CREATE TABLE IF NOT EXISTS raw_payloads (
    raw_key         TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    api_version     TEXT NOT NULL,
    origin          TEXT NOT NULL,
    destination     TEXT NOT NULL,
    departure_date  TEXT NOT NULL,  -- ISO 8601 date (YYYY-MM-DD)
    payload         TEXT NOT NULL,  -- raw provider response bytes/JSON, verbatim
    retrieved_at    TEXT NOT NULL,  -- ISO 8601 UTC instant this row was written
    expires_at      TEXT NOT NULL,  -- ISO 8601 UTC instant: retrieved_at + tiered TTL
    created_at      TEXT NOT NULL   -- ISO 8601 UTC instant of first INSERT (never updated)
);

CREATE INDEX IF NOT EXISTS idx_raw_payloads_expires_at
    ON raw_payloads (expires_at);

CREATE TABLE IF NOT EXISTS normalized_offers (
    normalized_key      TEXT PRIMARY KEY,
    raw_key             TEXT NOT NULL REFERENCES raw_payloads (raw_key),
    normalizer_version  TEXT NOT NULL,
    fx_source_id        TEXT NOT NULL,
    payload             TEXT NOT NULL,  -- normalized itineraries, serialized
    retrieved_at        TEXT NOT NULL,  -- ISO 8601 UTC instant this row was written
    expires_at          TEXT NOT NULL,  -- ISO 8601 UTC instant: retrieved_at + tiered TTL
    created_at          TEXT NOT NULL   -- ISO 8601 UTC instant of first INSERT (never updated)
);

CREATE INDEX IF NOT EXISTS idx_normalized_offers_raw_key
    ON normalized_offers (raw_key);

CREATE INDEX IF NOT EXISTS idx_normalized_offers_expires_at
    ON normalized_offers (expires_at);
