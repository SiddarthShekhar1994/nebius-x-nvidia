"""Persist and synchronize one Calendar event per opportunity deadline."""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .adapters import CalendarAdapter, IdempotencyConflict
from .application_activity import record_activity
from .deadlines import (
    DeadlineCandidate, DeadlineReviewPolicy, DeadlineReviewRequired,
    validated_deadline_at,
)
from .time import from_storage, to_storage
from .workflow import stable_key


_EXPLICIT_CHANGE = re.compile(
    r"\b(?:deadline\s+(?:has been\s+)?extended|extended\s+to|"
    r"deadline\s+moved\s+to|new\s+deadline)\b", re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CalendarSyncResult:
    status: str  # created, updated, unchanged, review
    event_id: str | None
    provider_event_id: str | None
    reason: str


def _connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _review(event_id: str | None, provider_id: str | None, reason: str) -> CalendarSyncResult:
    return CalendarSyncResult("review", event_id, provider_id, reason)


def sync_deadline_candidate(
    db_path: Path, calendar: CalendarAdapter, candidate: DeadlineCandidate, *,
    opportunity_id: str, now: datetime, application_id: str | None = None,
    policy: DeadlineReviewPolicy | None = None,
) -> CalendarSyncResult:
    """Create or update the stable event after validating source and change intent.

    A pending Keeper row is committed before a provider call. A failed create
    remains retryable under the same event ID; an existing event retains its
    old evidence until the provider confirms an update.
    """
    deadline_at = validated_deadline_at(candidate, now=now, policy=policy)
    if policy is not None and (
        policy.candidate_id != candidate.id or policy.confirmed_at != deadline_at
        or policy.approved_at > now
    ):
        raise DeadlineReviewRequired("review policy does not match this deadline")
    db_path = Path(db_path)
    if not db_path.is_file():
        raise ValueError("Keeper database does not exist")
    if not opportunity_id:
        raise ValueError("opportunity ID is required")
    event_id = stable_key("deadline", opportunity_id, "application-deadline")
    with closing(_connection(db_path)) as conn:
        opportunity = conn.execute(
            "SELECT organization, title FROM opportunities WHERE id = ?", (opportunity_id,),
        ).fetchone()
        if opportunity is None:
            raise ValueError("opportunity does not exist")
        if application_id is not None:
            application = conn.execute(
                "SELECT opportunity_id FROM applications WHERE id = ?", (application_id,),
            ).fetchone()
            if application != (opportunity_id,):
                raise ValueError("application does not belong to opportunity")
        source = conn.execute(
            "SELECT subject, body_excerpt, received_at FROM email_records WHERE id = ?",
            (candidate.source_email_id,),
        ).fetchone()
        if source is None:
            raise ValueError("source email is not stored")
        source_text = source[0] if candidate.source_field == "subject" else source[1] if candidate.source_field == "body_excerpt" else None
        if source_text is None or candidate.evidence not in source_text or candidate.raw_date not in candidate.evidence:
            raise ValueError("deadline evidence does not match stored email")
        existing = conn.execute(
            """SELECT application_id, source_email_id, deadline_at, evidence,
                      provider, provider_event_id
               FROM deadline_events WHERE id = ?""", (event_id,),
        ).fetchone()
        if existing is None:
            with conn:
                conn.execute(
                    """INSERT INTO deadline_events
                       (id, opportunity_id, application_id, source_email_id, deadline_at,
                        evidence, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event_id, opportunity_id, application_id, candidate.source_email_id,
                     to_storage(deadline_at), candidate.evidence, to_storage(now), to_storage(now)),
                )
            prior_provider_id = None
            prior_deadline = None
        else:
            prior_app, prior_email, prior_stamp, prior_evidence, provider, prior_provider_id = existing
            if prior_app is not None and application_id is not None and prior_app != application_id:
                raise IdempotencyConflict("deadline event is linked to another application")
            if provider not in (None, "google-calendar"):
                raise IdempotencyConflict("deadline event belongs to another provider")
            prior_deadline = from_storage(prior_stamp)
            if deadline_at != prior_deadline:
                if prior_provider_id is None:
                    return _review(event_id, None, "prior calendar creation is pending")
                if candidate.source_email_id == prior_email:
                    return _review(event_id, prior_provider_id, "same email proposes another deadline")
                older = conn.execute("SELECT received_at FROM email_records WHERE id = ?", (prior_email,)).fetchone()
                if older and from_storage(source[2]) <= from_storage(older[0]):
                    return _review(event_id, prior_provider_id, "older email cannot replace newer deadline")
                if deadline_at < prior_deadline and policy is None:
                    return _review(event_id, prior_provider_id, "shortened deadline needs review")
                if deadline_at > prior_deadline and not _EXPLICIT_CHANGE.search(candidate.evidence) and policy is None:
                    return _review(event_id, prior_provider_id, "changed deadline lacks explicit extension evidence")
            elif prior_provider_id is not None:
                if prior_app is None and application_id is not None:
                    with conn:
                        conn.execute(
                            "UPDATE deadline_events SET application_id = ? WHERE id = ?",
                            (application_id, event_id),
                        )
                        if prior_email and prior_evidence:
                            record_activity(
                                conn, application_id=application_id, kind="calendar_created",
                                source_email_id=prior_email, source_evidence=prior_evidence,
                                related_id=event_id, provider_id=prior_provider_id,
                                detail=f"Deadline scheduled for {prior_stamp}", occurred_at=now,
                            )
                # The provider may have accepted a previous update before the
                # final source/evidence write completed. Reconcile that gap
                # only from a newer, explicit change email.
                if candidate.source_email_id != prior_email and _EXPLICIT_CHANGE.search(candidate.evidence):
                    older = conn.execute(
                        "SELECT received_at FROM email_records WHERE id = ?", (prior_email,),
                    ).fetchone()
                    if older and from_storage(source[2]) > from_storage(older[0]):
                        with conn:
                            conn.execute(
                                """UPDATE deadline_events SET source_email_id = ?, evidence = ?,
                                   application_id = COALESCE(application_id, ?), updated_at = ?
                                   WHERE id = ?""",
                                (candidate.source_email_id, candidate.evidence,
                                 application_id, to_storage(now), event_id),
                            )
                            if application_id is not None:
                                record_activity(
                                    conn, application_id=application_id, kind="calendar_updated",
                                    source_email_id=candidate.source_email_id,
                                    source_evidence=candidate.evidence, related_id=event_id,
                                    provider_id=prior_provider_id,
                                    detail=f"Deadline moved to {to_storage(deadline_at)}",
                                    occurred_at=now,
                                )
                return CalendarSyncResult("unchanged", event_id, prior_provider_id, "deadline already scheduled")
    summary = f"Application deadline: {opportunity[0]} — {opportunity[1]}"
    provider_id = calendar.upsert_deadline(
        event_id, deadline_at, summary, idempotency_key=event_id,
        provider_event_id=prior_provider_id, review_policy=policy,
    )
    with closing(_connection(db_path)) as conn, conn:
        conn.execute(
            """UPDATE deadline_events
               SET application_id = COALESCE(application_id, ?), source_email_id = ?,
                   deadline_at = ?, evidence = ?, provider = 'google-calendar',
                   provider_event_id = ?, updated_at = ? WHERE id = ?""",
            (application_id, candidate.source_email_id, to_storage(deadline_at),
             candidate.evidence, provider_id, to_storage(now), event_id),
        )
        if application_id is not None:
            record_activity(
                conn, application_id=application_id,
                kind="calendar_created" if prior_provider_id is None else "calendar_updated",
                source_email_id=candidate.source_email_id, source_evidence=candidate.evidence,
                related_id=event_id, provider_id=provider_id,
                detail=f"Deadline scheduled for {to_storage(deadline_at)}" if prior_provider_id is None
                else f"Deadline moved to {to_storage(deadline_at)}",
                occurred_at=now,
            )
    return CalendarSyncResult("created" if prior_provider_id is None else "updated",
                              event_id, provider_id, "calendar event synchronized")
