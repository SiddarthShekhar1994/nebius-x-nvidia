"""Persist human approval before synchronizing an exact reviewed page deadline."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .adapters import CalendarAdapter, IdempotencyConflict
from .calendar_deadlines import CalendarSyncResult
from .contracts import Eligibility
from .deadlines import DeadlineReviewPolicy, DeadlineReviewRequired, validate_calendar_instant
from .posting_reviews import get_posting_review, parse_posting_evidence
from .radar_deadlines import posting_deadline_revision, sync_posting_deadline
from .time import from_storage, to_storage
from .workflow import stable_key


def _prepare(conn: sqlite3.Connection, review_id: str, now: datetime):
    if now.tzinfo is not timezone.utc:
        raise ValueError("now must be UTC")
    review = get_posting_review(conn, review_id)
    if review is None:
        raise ValueError("posting review does not exist")
    latest = conn.execute(
        """SELECT id FROM posting_reviews WHERE opportunity_id = ?
           ORDER BY reviewed_at DESC, rowid DESC LIMIT 1""", (review["opportunity_id"],),
    ).fetchone()
    if latest != (review_id,) or review["reviewed_at"] > now:
        raise DeadlineReviewRequired("deadline needs the latest completed page review")
    eligibility = conn.execute("SELECT eligibility FROM opportunities WHERE id = ?",
                               (review["opportunity_id"],)).fetchone()
    if review["eligibility"] is not Eligibility.ELIGIBLE or eligibility != ("eligible",):
        raise DeadlineReviewRequired("confirmed eligibility is required before Calendar creation")
    evidence = parse_posting_evidence({key: review[key] for key in (
        "opportunity_id", "source_url", "source_excerpt", "requirements", "deadline_timestamp",
    )})
    if evidence.deadline is None:
        raise DeadlineReviewRequired("review has no explicit application deadline")
    validate_calendar_instant(evidence.deadline.deadline_at, now=now)
    if evidence.deadline.deadline_at <= now:
        raise DeadlineReviewRequired("reviewed deadline is no longer in the future")
    event_id = stable_key("deadline", review["opportunity_id"], "application-deadline")
    existing = conn.execute(
        "SELECT deadline_at, evidence, provider, provider_event_id FROM deadline_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if existing and existing[2] not in (None, "google-calendar"):
        raise IdempotencyConflict("deadline belongs to another Calendar provider")
    return review, evidence.deadline, event_id, existing


def calendar_review_plan(conn: sqlite3.Connection, review_id: str, *, now: datetime) -> dict:
    """Inspect the exact proposed action without recording approval or calling Calendar."""
    review, deadline, event_id, existing = _prepare(conn, review_id, now)
    source = f"{review['source_url']} | {review['source_excerpt'].strip()}"
    if existing is None:
        action, reason = "create", "approve this reviewed deadline to create one Calendar event"
    elif existing[3] is None and (from_storage(existing[0]) != deadline.deadline_at or existing[1] != source):
        action, reason = "review", "prior Calendar creation is pending with different evidence"
    elif from_storage(existing[0]) == deadline.deadline_at and existing[1] == source and existing[3]:
        action, reason = "unchanged", "the same reviewed deadline is already scheduled"
    else:
        action, reason = "update" if existing[3] else "create", "reuse the existing Calendar event identity"
    return {"review_id": review_id, "opportunity_id": review["opportunity_id"],
            "source_url": review["source_url"], "source_excerpt": review["source_excerpt"],
            "deadline_at": deadline.deadline_at, "action": action, "reason": reason,
            "event_id": event_id, "provider_event_id": existing[3] if existing else None,
            "approved_by": review["approved_by"], "approved_at": review["approved_at"]}


def schedule_reviewed_deadline(db_path: Path, calendar: CalendarAdapter, review_id: str, *,
                               approved_by: str, reason: str, now: datetime) -> CalendarSyncResult:
    """Approve this immutable review revision, then create, update, or retry one event."""
    if not approved_by.strip() or not reason.strip() or len(approved_by) > 200 or len(reason) > 1000:
        raise ValueError("approval requires a reviewer and a short reason")
    db_path = Path(db_path)
    if not db_path.is_file():
        raise ValueError("Keeper database does not exist")
    with closing(sqlite3.connect(db_path, timeout=10)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        review, deadline, event_id, existing = _prepare(conn, review_id, now)
        source = f"{review['source_url']} | {review['source_excerpt'].strip()}"
        if existing and existing[3] is None and (
            from_storage(existing[0]) != deadline.deadline_at or existing[1] != source
        ):
            return CalendarSyncResult("review", event_id, None,
                                      "prior Calendar creation is pending with different evidence")
        if review["approved_by"] is None:
            conn.execute(
                """UPDATE posting_reviews SET approved_by = ?, approved_at = ?, approval_reason = ?
                   WHERE id = ?""", (approved_by, to_storage(now), reason, review_id),
            )
            approved_at = now
        else:
            if (review["approved_by"], review["approval_reason"]) != (approved_by, reason):
                raise DeadlineReviewRequired("retry must preserve the saved approval")
            approved_at = review["approved_at"]
        policy = DeadlineReviewPolicy(
            posting_deadline_revision(review["opportunity_id"], deadline), approved_by,
            reason, approved_at, deadline.deadline_at,
        )
        if approved_at > now:
            raise DeadlineReviewRequired("approval cannot be in the future")
    return sync_posting_deadline(
        db_path, calendar, opportunity_id=review["opportunity_id"], deadline=deadline,
        now=now, policy=policy,
    )
