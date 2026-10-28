-- Keep evidence with every application history entry, including repeated status evidence.
ALTER TABLE application_status_changes ADD COLUMN source_evidence TEXT NOT NULL DEFAULT '';
ALTER TABLE application_status_changes ADD COLUMN correction_id TEXT REFERENCES correction_records(id);
ALTER TABLE application_status_changes ADD COLUMN decision_outcome TEXT;
UPDATE application_status_changes SET source_evidence = reason WHERE source_evidence = '';
UPDATE application_status_changes
SET decision_outcome = (SELECT a.decision_outcome FROM applications a WHERE a.id = application_id)
WHERE status = 'decision';
CREATE TRIGGER application_change_requires_evidence
BEFORE INSERT ON application_status_changes
WHEN TRIM(NEW.source_evidence) = ''
BEGIN
    SELECT RAISE(ABORT, 'application change requires source evidence');
END;
CREATE UNIQUE INDEX uq_application_change_email
    ON application_status_changes(application_id, source_email_id)
    WHERE source_email_id IS NOT NULL;
CREATE UNIQUE INDEX uq_application_change_correction
    ON application_status_changes(correction_id)
    WHERE correction_id IS NOT NULL;
