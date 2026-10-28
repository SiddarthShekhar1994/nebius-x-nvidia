import unittest
from dataclasses import replace
from datetime import datetime, timezone

from keeper_core.contracts import EmailRecord, IngestionMetadata, TriageBucket, TriageResult
from keeper_core.deadlines import (
    DeadlineCandidate, DeadlineReviewPolicy, DeadlineReviewRequired, extract_deadline_candidates,
    validated_deadline_at,
)


NOW = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)


def email(body, subject="Application deadline"):
    return EmailRecord("gmail:deadline", "gmail", "deadline", None,
                       "recruiter@example.test", subject, NOW, body, (),
                       "gmail:deadline", IngestionMetadata(NOW, "run", "fixture"))


def triage(source, *, career=True, bucket=TriageBucket.DUE):
    return TriageResult("triage:deadline", source.id, bucket, "Relevant career deadline",
                        career, (), None, None, None, NOW)


class DeadlineCandidateTest(unittest.TestCase):
    def test_explicit_future_instant_has_source_and_needs_no_assumption(self):
        source = email("Apply by 2026-11-02T09:30:00-08:00 for the internship.")
        candidates = extract_deadline_candidates(source, triage(source), now=NOW)
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate.source_email_id, source.id)
        self.assertEqual(candidate.source_field, "body_excerpt")
        self.assertEqual(candidate.source_reference, "gmail:deadline")
        self.assertIn("2026-11-02T09:30:00-08:00", candidate.evidence)
        self.assertEqual(candidate.flags, ())
        self.assertEqual(validated_deadline_at(candidate, now=NOW),
                         datetime(2026, 11, 2, 17, 30, tzinfo=timezone.utc))

    def test_date_only_and_missing_timezone_need_review(self):
        source = email("Apply by 2026-11-02. Submit by 2026-11-03T09:30.")
        candidates = extract_deadline_candidates(source, triage(source), now=NOW)
        self.assertEqual(len(candidates), 2)
        self.assertIn("time_missing", candidates[0].flags)
        self.assertIn("timezone_missing", candidates[1].flags)
        self.assertIn("multiple_candidates", candidates[0].flags)
        for candidate in candidates:
            with self.assertRaises(DeadlineReviewRequired):
                validated_deadline_at(candidate, now=NOW)
        policy = DeadlineReviewPolicy(candidates[0].id, "user:1", "Confirmed exact deadline with recruiter", NOW,
                                      datetime(2026, 11, 2, 17, 30, tzinfo=timezone.utc))
        self.assertEqual(validated_deadline_at(candidates[0], now=NOW, policy=policy),
                         policy.confirmed_at)
        with self.assertRaises(DeadlineReviewRequired):
            validated_deadline_at(candidates[1], now=NOW, policy=policy)

    def test_named_zone_checks_dst_and_conflicting_offset(self):
        ambiguous = email("Apply by 2026-11-01T01:30 America/Los_Angeles.")
        candidate = extract_deadline_candidates(ambiguous, triage(ambiguous), now=NOW)[0]
        self.assertIn("ambiguous_local_time", candidate.flags)
        with self.assertRaises(DeadlineReviewRequired):
            validated_deadline_at(candidate, now=NOW)
        nonexistent = email("Apply by 2026-03-08T02:30 America/Los_Angeles.")
        candidate = extract_deadline_candidates(nonexistent, triage(nonexistent), now=NOW)[0]
        self.assertIn("nonexistent_local_time", candidate.flags)
        conflict = email("Apply by 2026-11-02T09:30:00-07:00 America/Los_Angeles.")
        candidate = extract_deadline_candidates(conflict, triage(conflict), now=NOW)[0]
        self.assertIn("timezone_conflict", candidate.flags)
        valid = email("Apply by 2026-11-02T09:30 America/Los_Angeles.")
        candidate = extract_deadline_candidates(valid, triage(valid), now=NOW)[0]
        self.assertEqual(candidate.flags, ())
        self.assertEqual(validated_deadline_at(candidate, now=NOW),
                         datetime(2026, 11, 2, 17, 30, tzinfo=timezone.utc))
        vague_label = email("Apply by 2026-11-01T09:30:00-07:00 Pacific.")
        candidate = extract_deadline_candidates(vague_label, triage(vague_label), now=NOW)[0]
        self.assertIn("timezone_label_unverified", candidate.flags)

    def test_past_deadline_requires_explicit_past_policy(self):
        source = email("Apply by 2026-10-26T09:30:00Z.")
        candidate = extract_deadline_candidates(source, triage(source), now=NOW)[0]
        self.assertIn("past", candidate.flags)
        with self.assertRaises(DeadlineReviewRequired):
            validated_deadline_at(candidate, now=NOW)
        approved = DeadlineReviewPolicy(candidate.id, "user:1", "Schedule historical reminder after review", NOW,
                                        candidate.deadline_at, allow_past=True)
        self.assertEqual(validated_deadline_at(candidate, now=NOW, policy=approved), candidate.deadline_at)
        with self.assertRaises(DeadlineReviewRequired):
            validated_deadline_at(candidate, now=NOW, policy=replace(approved, allow_past=False))
        with self.assertRaises(DeadlineReviewRequired):
            validated_deadline_at(candidate, now=NOW,
                                  policy=replace(approved, approved_at=datetime(2026, 10, 28, tzinfo=timezone.utc)))

    def test_irrelevant_mail_and_invalid_dates(self):
        source = email("Apply by 2026-11-02T09:30:00Z.")
        self.assertEqual(extract_deadline_candidates(source, triage(source, career=False), now=NOW), ())
        self.assertEqual(extract_deadline_candidates(source, triage(source, bucket=TriageBucket.JUNK), now=NOW), ())
        invalid = email("Apply by 2026-02-30T09:30:00Z.")
        candidate = extract_deadline_candidates(invalid, triage(invalid), now=NOW)[0]
        self.assertIn("invalid_date", candidate.flags)
        self.assertIsNone(candidate.deadline_at)

    def test_natural_language_is_retained_as_uncertain_evidence(self):
        source = email("Applications close November 2, 2026 at 9:30 AM Pacific.")
        candidate = extract_deadline_candidates(source, triage(source), now=NOW)[0]
        self.assertIn("natural_language_date", candidate.flags)
        self.assertIn("Pacific", candidate.evidence)
        self.assertIsNone(candidate.deadline_at)
        with self.assertRaises(DeadlineReviewRequired):
            validated_deadline_at(candidate, now=NOW)

    def test_subject_context_without_body_deadline_cue_needs_review(self):
        source = email("2026-11-02T09:30:00Z is the date shown in the portal.")
        candidate = extract_deadline_candidates(source, triage(source), now=NOW)[0]
        self.assertIn("subject_context_only", candidate.flags)
        with self.assertRaises(DeadlineReviewRequired):
            validated_deadline_at(candidate, now=NOW)

    def test_uncertain_relevance_preserves_candidate_but_blocks_calendar(self):
        source = email("Apply by 2026-11-02T09:30:00Z.")
        candidate = extract_deadline_candidates(
            source, triage(source, career=None, bucket=None), now=NOW,
        )[0]
        self.assertIn("relevance_unverified", candidate.flags)
        self.assertIn("triage_review", candidate.flags)
        with self.assertRaises(DeadlineReviewRequired):
            validated_deadline_at(candidate, now=NOW)

    def test_candidate_contract_rejects_naive_instant(self):
        with self.assertRaises(ValueError):
            DeadlineCandidate("candidate:1", "gmail:1", "body_excerpt", "gmail:1",
                              "2026-11-02T09:30", "Apply by 2026-11-02T09:30",
                              datetime(2026, 11, 2, 9, 30), ())


if __name__ == "__main__":
    unittest.main()
