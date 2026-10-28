CREATE TABLE radar_digest_snapshots (
    run_id TEXT NOT NULL,
    recipient_reference TEXT NOT NULL,
    body TEXT NOT NULL,
    opportunity_ids_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL CHECK (created_at GLOB '*Z'),
    PRIMARY KEY (run_id, recipient_reference)
);
