ALTER TABLE radar_digest_snapshots
ADD COLUMN policy_version TEXT NOT NULL DEFAULT 'legacy';
