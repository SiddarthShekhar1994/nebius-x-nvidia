import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keeper_core.application_messages import detect_application_signal, process_application_email
from keeper_core.applications import create_application, get_application
from keeper_core.contracts import ApplicationStatus, EmailRecord, IngestionMetadata
from keeper_core.db import migrate
from keeper_core.ingest import insert_email_once


WHEN = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
STAMP = "2026-10-26T12:00:00.000000Z"


class ApplicationEmailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "keeper.db"
        self.conn = sqlite3.connect(self.path)
        migrate(self.path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(
            """INSERT INTO opportunities
               (id, dedupe_key, canonical_url, organization, title, source,
                discovered_at, eligibility)
               VALUES ('opp:1', 'key:1', 'https://example.test/job/1',
                       'Example, Inc.', 'Software Engineer Intern', 'fixture', ?, 'unverified')""",
            (STAMP,),
        )
        self.app = create_application(self.conn, "opp:1", created_at=WHEN,
                                      source_evidence="https://example.test/job/1")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def message(self, number, subject, body):
        at = WHEN + timedelta(hours=number)
        record = EmailRecord(f"gmail:{number}", "gmail", str(number), None,
                             "recruiting@example.test", subject, at, body, (), None,
                             IngestionMetadata(at, "run:1", "fixture"))
        insert_email_once(self.conn, record)
        return record

    def test_one_application_moves_through_confirmation_interview_rejection(self):
        messages = (
            self.message(1, "Application received", "Example Inc received your application for Software Engineer Intern."),
            self.message(2, "Interview invitation", "Example Inc would like to interview you for Software Engineer Intern."),
            self.message(3, "Application update", "Example Inc: unfortunately, we will not be moving forward with your application for Software Engineer Intern."),
        )
        expected = (ApplicationStatus.APPLIED, ApplicationStatus.INTERVIEW, ApplicationStatus.DECISION)
        for message, status in zip(messages, expected):
            result = process_application_email(self.conn, message)
            self.assertEqual(result.status, "updated")
            self.assertEqual(result.application.status, status)
            self.assertEqual(result.application.id, self.app.id)
            self.assertEqual(process_application_email(self.conn, message).status, "reused")
        final = get_application(self.conn, self.app.id)
        self.assertEqual(final.decision_outcome, "rejected")
        self.assertEqual(final.status_history[-1].decision_outcome, "rejected")
        self.assertEqual(len(final.status_history), 4)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM applications").fetchone()[0], 1)

    def test_offer_preserves_outcome_and_ambiguous_role_goes_to_review(self):
        self.conn.execute(
            """INSERT INTO opportunities
               (id, dedupe_key, canonical_url, organization, title, source,
                discovered_at, eligibility)
               VALUES ('opp:2', 'key:2', 'https://example.test/job/2',
                       'Example Inc', 'Data Science Intern', 'fixture', ?, 'unverified')""",
            (STAMP,),
        )
        create_application(self.conn, "opp:2", created_at=WHEN, source_evidence="Second posting")
        ambiguous = self.message(1, "Application update", "Example Inc is pleased to offer you a position.")
        self.assertEqual(process_application_email(self.conn, ambiguous).status, "review")
        self.assertEqual(get_application(self.conn, self.app.id).status, ApplicationStatus.FOUND)
        offered = self.message(2, "Job offer", "Example Inc is pleased to offer you the Software Engineer Intern position.")
        result = process_application_email(self.conn, offered)
        self.assertEqual(result.status, "updated")
        self.assertEqual(result.application.decision_outcome, "offered")

    def test_weak_or_conflicting_language_does_not_change_state(self):
        weak = self.message(1, "Interview tips", "Example Inc Software Engineer Intern: prepare for your interview.")
        self.assertEqual(process_application_email(self.conn, weak).status, "ignored")
        conflicting = self.message(2, "Decision", "Example Inc is pleased to offer you Software Engineer Intern. Unfortunately, you were not selected.")
        self.assertEqual(process_application_email(self.conn, conflicting).status, "review")
        self.assertEqual(get_application(self.conn, self.app.id).status, ApplicationStatus.FOUND)

    def test_interview_offer_phrase_is_not_a_job_offer(self):
        invitation = self.message(
            1, "Next step", "Example Inc is pleased to offer you an interview for Software Engineer Intern."
        )
        result = process_application_email(self.conn, invitation)
        self.assertEqual(result.status, "updated")
        self.assertEqual(result.application.status, ApplicationStatus.INTERVIEW)
        self.assertIsNone(result.application.decision_outcome)

    def test_contradictory_and_backwards_updates_require_review(self):
        interview = self.message(
            1, "Interview invitation", "Example Inc invites you to interview for Software Engineer Intern."
        )
        self.assertEqual(process_application_email(self.conn, interview).status, "updated")
        late_confirmation = self.message(
            2, "Application received", "Example Inc received your application for Software Engineer Intern."
        )
        self.assertEqual(process_application_email(self.conn, late_confirmation).status, "review")
        rejection = self.message(
            3, "Decision", "Example Inc did not select you for Software Engineer Intern. You were not selected."
        )
        self.assertEqual(process_application_email(self.conn, rejection).status, "updated")
        offer = self.message(
            4, "Offer", "Example Inc is pleased to offer you Software Engineer Intern."
        )
        self.assertEqual(process_application_email(self.conn, offer).status, "review")
        final = get_application(self.conn, self.app.id)
        self.assertEqual(final.decision_outcome, "rejected")
        self.assertEqual(len(final.status_history), 3)

    def test_negated_offer_is_not_a_decision(self):
        message = self.message(
            1, "Information", "Example Inc Software Engineer Intern: this is not an offer of employment."
        )
        self.assertEqual(process_application_email(self.conn, message).status, "ignored")

    def test_unspecified_decision_requires_review(self):
        message = self.message(
            1, "Application decision", "Example Inc has made a decision on your application for Software Engineer Intern."
        )
        self.assertEqual(process_application_email(self.conn, message).status, "review")
        self.assertEqual(get_application(self.conn, self.app.id).status, ApplicationStatus.FOUND)

    def test_organization_and_title_are_required_in_message(self):
        subject_only = self.message(1, "Example Inc Software Engineer Intern application received", "Thank you.")
        self.assertEqual(process_application_email(self.conn, subject_only).status, "review")
        unknown = self.message(2, "Application received", "Other Co received your application for Software Engineer Intern.")
        self.assertEqual(process_application_email(self.conn, unknown).status, "review")
        self.assertEqual(get_application(self.conn, self.app.id).status, ApplicationStatus.FOUND)

    def test_repeated_ingestion_and_changed_source_do_not_duplicate_history(self):
        message = self.message(
            1, "Application received", "Example Inc received your application for Software Engineer Intern."
        )
        self.assertEqual(process_application_email(self.conn, message).status, "updated")
        self.assertEqual(insert_email_once(self.conn, message), message.id)
        self.assertEqual(process_application_email(self.conn, message).status, "reused")
        changed = replace(message, body_excerpt="Example Inc invites you to interview for Software Engineer Intern.")
        self.assertEqual(process_application_email(self.conn, changed).status, "review")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM email_records").fetchone()[0], 1)
        self.assertEqual(len(get_application(self.conn, self.app.id).status_history), 2)
        self.conn.commit()
        with closing(sqlite3.connect(self.path)) as restarted:
            restarted.execute("PRAGMA foreign_keys = ON")
            self.assertEqual(insert_email_once(restarted, message), message.id)
            self.assertEqual(process_application_email(restarted, message).status, "reused")
            self.assertEqual(restarted.execute("SELECT count(*) FROM application_status_changes").fetchone()[0], 2)

    def test_out_of_order_messages_do_not_regress_or_add_history(self):
        interview = self.message(
            2, "Interview invitation", "Example Inc invites you to interview for Software Engineer Intern."
        )
        confirmation = self.message(
            1, "Application received", "Example Inc received your application for Software Engineer Intern."
        )
        self.assertEqual(process_application_email(self.conn, interview).status, "updated")
        self.assertEqual(process_application_email(self.conn, confirmation).status, "ignored")
        app = get_application(self.conn, self.app.id)
        self.assertEqual(app.status, ApplicationStatus.INTERVIEW)
        self.assertEqual(len(app.status_history), 2)

    def test_multiple_roles_one_company_and_ambiguous_subject(self):
        self.conn.execute(
            """INSERT INTO opportunities
               (id, dedupe_key, canonical_url, organization, title, source,
                discovered_at, eligibility)
               VALUES ('opp:2', 'key:2', 'https://example.test/job/2',
                       'Example Inc', 'Data Science Intern', 'fixture', ?, 'unverified')""",
            (STAMP,),
        )
        other = create_application(self.conn, "opp:2", created_at=WHEN, source_evidence="Second posting")
        vague = self.message(
            1, "Example Inc Software Engineer Intern — interview invitation", "Please check your portal."
        )
        self.assertEqual(process_application_email(self.conn, vague).status, "review")
        both = self.message(
            2, "Interview invitation", "Example Inc invites you to interview for Software Engineer Intern or Data Science Intern."
        )
        self.assertEqual(process_application_email(self.conn, both).status, "review")
        clear = self.message(
            3, "Interview invitation", "Example Inc invites you to interview for Data Science Intern."
        )
        result = process_application_email(self.conn, clear)
        self.assertEqual(result.status, "updated")
        self.assertEqual(result.application.id, other.id)
        self.assertEqual(get_application(self.conn, self.app.id).status, ApplicationStatus.FOUND)


if __name__ == "__main__":
    unittest.main()
