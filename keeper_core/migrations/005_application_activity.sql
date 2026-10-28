ALTER TABLE draft_records ADD COLUMN review_flags_json TEXT NOT NULL DEFAULT '[]';

CREATE TABLE application_activity (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    kind TEXT NOT NULL CHECK (kind IN ('calendar_created', 'calendar_updated', 'gmail_draft')),
    source_email_id TEXT NOT NULL REFERENCES email_records(id),
    source_evidence TEXT NOT NULL CHECK (length(source_evidence) > 0),
    related_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    detail TEXT NOT NULL,
    review_flags_json TEXT NOT NULL DEFAULT '[]',
    occurred_at TEXT NOT NULL CHECK (occurred_at GLOB '*Z')
);
CREATE INDEX ix_application_activity_history ON application_activity(application_id, occurred_at);
CREATE INDEX ix_application_activity_email ON application_activity(source_email_id);
