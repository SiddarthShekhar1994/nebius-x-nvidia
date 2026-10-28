CREATE TABLE opportunity_observations (
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id),
    run_id TEXT NOT NULL,
    observed_url TEXT NOT NULL,
    observed_at TEXT NOT NULL CHECK (observed_at GLOB '*Z'),
    source TEXT NOT NULL,
    score INTEGER CHECK (score IS NULL OR score BETWEEN 1 AND 10),
    explanation TEXT,
    eligibility TEXT NOT NULL CHECK (eligibility IN ('eligible', 'ineligible', 'unverified')),
    eligibility_reason TEXT,
    PRIMARY KEY (opportunity_id, run_id)
);
CREATE INDEX idx_opportunity_observations_run ON opportunity_observations(run_id);
