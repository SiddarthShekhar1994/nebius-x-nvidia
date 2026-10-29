CREATE TABLE posting_reviews (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id),
    evidence_json TEXT NOT NULL,
    reviewed_by TEXT NOT NULL,
    reviewed_at TEXT NOT NULL CHECK (reviewed_at GLOB '*Z'),
    eligibility TEXT NOT NULL CHECK (eligibility IN ('eligible', 'ineligible', 'unverified')),
    eligibility_reason TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT CHECK (approved_at IS NULL OR approved_at GLOB '*Z'),
    approval_reason TEXT,
    CHECK (
        (approved_by IS NULL AND approved_at IS NULL AND approval_reason IS NULL)
        OR (approved_by IS NOT NULL AND approved_at IS NOT NULL AND approval_reason IS NOT NULL)
    )
);
CREATE INDEX idx_posting_reviews_opportunity_time
    ON posting_reviews(opportunity_id, reviewed_at, id);
