import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keeper_core.applications import (
    apply_application_transition, apply_manual_correction, apply_message_transition,
    create_application, get_application,
    list_applications, resolve_message_application,
)
from keeper_core.contracts import ApplicationStatus, CorrectionRecord
from keeper_core.db import migrate
from keeper_core.workflow import TransitionConflict


WHEN = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
STAMP = "2026-10-26T12:00:00.000000Z"


class ApplicationRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "keeper.db"
        migrate(path)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.add_opportunity("opp:one", "Example, Inc.", "Software Engineer Intern")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def add_opportunity(self, record_id, organization, title):
        self.conn.execute(
            """INSERT INTO opportunities
               (id, dedupe_key, canonical_url, organization, title, source, discovered_at, eligibility)
               VALUES (?, ?, ?, ?, ?, 'fixture', ?, 'unverified')""",
            (record_id, record_id, f"https://example.test/{record_id}", organization, title, STAMP),
        )

    def add_email(self, record_id):
        self.conn.execute(
            """INSERT INTO email_records
               (id, provider, provider_id, sender, subject, received_at, body_excerpt,
                ingested_at, ingestion_run_id, ingestion_source)
               VALUES (?, 'gmail', ?, 'recruiter@example.test', 'Update', ?, '', ?, 'run', 'fixture')""",
            (record_id, record_id, STAMP, STAMP),
        )

    def test_create_and_replay_preserve_one_application_with_evidence(self):
        created = create_application(self.conn, "opp:one", created_at=WHEN,
                                     source_evidence="https://example.test/opp:one")
        self.assertEqual(create_application(self.conn, "opp:one", created_at=WHEN,
                                            source_evidence="https://example.test/opp:one").id, created.id)
        self.assertEqual(created.status_history[0].source_evidence, "https://example.test/opp:one")
        for index, status in enumerate((ApplicationStatus.APPLIED, ApplicationStatus.INTERVIEW,
                                        ApplicationStatus.DECISION), 1):
            email_id = f"gmail:{index}"
            self.add_email(email_id)
            changed_at = WHEN + timedelta(hours=index)
            result = apply_application_transition(
                self.conn, created.id, status, changed_at=changed_at,
                reason=f"Status {index}", source_evidence=f"Email {index}: explicit status",
                source_email_id=email_id,
                decision_outcome="rejected" if status is ApplicationStatus.DECISION else None,
            )
            self.assertEqual(result.status, status)
            replay = apply_application_transition(
                self.conn, created.id, status, changed_at=changed_at,
                reason=f"Status {index}", source_evidence=f"Email {index}: explicit status",
                source_email_id=email_id,
                decision_outcome="rejected" if status is ApplicationStatus.DECISION else None,
            )
            self.assertEqual(replay, result)
        self.assertEqual(len(list_applications(self.conn)), 1)
        self.assertEqual(len(get_application(self.conn, created.id).status_history), 4)
        self.assertEqual(get_application(self.conn, created.id).status_history[-1].decision_outcome, "rejected")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_status_changes").fetchone()[0], 4)
        self.assertEqual(len(get_application(self.conn, created.id).source_email_ids), 3)

    def test_same_status_email_has_auditable_evidence_and_conflicts_still_fail(self):
        app = create_application(self.conn, "opp:one", created_at=WHEN, source_evidence="Posting URL")
        self.add_email("gmail:one")
        self.add_email("gmail:two")
        apply_application_transition(self.conn, app.id, ApplicationStatus.APPLIED,
                                     changed_at=WHEN + timedelta(hours=1), reason="Confirmed",
                                     source_evidence="Confirmation text", source_email_id="gmail:one")
        attached = apply_application_transition(self.conn, app.id, ApplicationStatus.APPLIED,
                                                changed_at=WHEN + timedelta(hours=2), reason="Confirmed again",
                                                source_evidence="Second confirmation", source_email_id="gmail:two")
        self.assertEqual(len(attached.status_history), 3)
        self.assertEqual(attached.status_history[-1].source_evidence, "Second confirmation")
        with self.assertRaises(TransitionConflict):
            apply_application_transition(self.conn, app.id, ApplicationStatus.INTERVIEW,
                                         changed_at=WHEN + timedelta(hours=3), reason="Wrong replay",
                                         source_evidence="Changed interpretation", source_email_id="gmail:two")
        with self.assertRaises(TransitionConflict):
            apply_application_transition(self.conn, app.id, ApplicationStatus.FOUND,
                                         changed_at=WHEN + timedelta(hours=3), reason="Backwards",
                                         source_evidence="Old mail", source_email_id="gmail:one")

    def test_identity_requires_unique_organization_and_title(self):
        first = create_application(self.conn, "opp:one", created_at=WHEN, source_evidence="Posting URL")
        self.add_opportunity("opp:two", "Example Inc", "Data Science Intern")
        second = create_application(self.conn, "opp:two", created_at=WHEN, source_evidence="Second posting")
        match = resolve_message_application(self.conn, organization=" EXAMPLE INC ", title="Software-Engineer Intern")
        self.assertEqual((match.status, match.application_id), ("matched", first.id))
        missing = resolve_message_application(self.conn, organization="Example Inc", title=None)
        self.assertEqual((missing.status, set(missing.candidate_application_ids)),
                         ("review", {first.id, second.id}))
        self.assertEqual(resolve_message_application(
            self.conn, organization="Example Inc", title="Unknown Intern").status, "review")
        self.assertEqual(resolve_message_application(
            self.conn, organization="Other", title="Software Engineer Intern").status, "unmatched")
        self.add_opportunity("opp:three", "EXAMPLE INC", "Software Engineer Intern")
        create_application(self.conn, "opp:three", created_at=WHEN, source_evidence="Duplicate title")
        self.assertEqual(resolve_message_application(
            self.conn, organization="Example Inc", title="Software Engineer Intern").status, "review")

    def test_ambiguous_message_never_changes_an_application(self):
        first = create_application(self.conn, "opp:one", created_at=WHEN, source_evidence="Posting URL")
        self.add_opportunity("opp:two", "Example Inc", "Data Science Intern")
        create_application(self.conn, "opp:two", created_at=WHEN, source_evidence="Second posting")
        self.add_email("gmail:ambiguous")
        resolution, updated = apply_message_transition(
            self.conn, organization="Example Inc", title=None,
            status=ApplicationStatus.APPLIED, changed_at=WHEN + timedelta(hours=1),
            reason="Application confirmation", source_evidence="Thanks for applying",
            source_email_id="gmail:ambiguous",
        )
        self.assertEqual(resolution.status, "review")
        self.assertIsNone(updated)
        self.assertEqual(get_application(self.conn, first.id).status, ApplicationStatus.FOUND)

    def test_manual_correction_requires_persisted_approval_and_records_evidence(self):
        app = create_application(self.conn, "opp:one", created_at=WHEN, source_evidence="Posting URL")
        at = WHEN + timedelta(hours=1)
        correction = CorrectionRecord("correction:1", "application", app.id, "Status corrected",
                                      WHEN, "approved", approved_at=at, actor_reference="user:1")
        with self.assertRaisesRegex(ValueError, "persisted"):
            apply_application_transition(self.conn, app.id, ApplicationStatus.APPLIED,
                                         changed_at=at, reason="User correction",
                                         source_evidence="User approved the correction",
                                         correction=correction)
        self.conn.execute(
            """INSERT INTO correction_records
               (id, target_type, target_id, correction, created_at, approval_status,
                approved_at, actor_reference) VALUES (?, 'application', ?, ?, ?, 'approved', ?, ?)""",
            (correction.id, app.id, correction.correction, STAMP,
             "2026-10-26T13:00:00.000000Z", correction.actor_reference),
        )
        updated = apply_application_transition(self.conn, app.id, ApplicationStatus.APPLIED,
                                               changed_at=at, reason="User correction",
                                               source_evidence="User approved the correction",
                                               correction=correction)
        self.assertEqual(updated.status_history[-1].correction_id, correction.id)
        self.assertEqual(updated.status_history[-1].source_evidence, "User approved the correction")

    def test_failed_email_reference_does_not_partially_update(self):
        app = create_application(self.conn, "opp:one", created_at=WHEN, source_evidence="Posting URL")
        with self.assertRaises(sqlite3.IntegrityError):
            apply_application_transition(self.conn, app.id, ApplicationStatus.APPLIED,
                                         changed_at=WHEN + timedelta(hours=1), reason="Confirmed",
                                         source_evidence="Missing email record", source_email_id="missing")
        self.assertEqual(get_application(self.conn, app.id).status, ApplicationStatus.FOUND)
        self.assertEqual(len(get_application(self.conn, app.id).status_history), 1)

    def test_stale_approval_rolls_back_correction_record(self):
        app = create_application(self.conn, "opp:one", created_at=WHEN, source_evidence="Posting URL")
        with self.assertRaises(TransitionConflict):
            apply_manual_correction(
                self.conn, app.id, ApplicationStatus.APPLIED,
                correction_id="correction:old", actor_reference="user:1",
                reason="Old approval", source_evidence="Review note",
                approved_at=WHEN - timedelta(hours=1),
            )
        self.assertEqual(self.conn.execute("SELECT count(*) FROM correction_records").fetchone()[0], 0)
        self.assertEqual(len(get_application(self.conn, app.id).status_history), 1)


if __name__ == "__main__":
    unittest.main()
