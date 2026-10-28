"""Schedule only explicit, source-backed application-page deadlines."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .adapters import CalendarAdapter, IdempotencyConflict
from .calendar_deadlines import CalendarSyncResult
from .deadlines import DeadlineReviewRequired, validate_calendar_instant
from .time import from_storage, to_storage
from .workflow import canonical_url, stable_key


@dataclass(frozen=True, slots=True)
class VerifiedPostingDeadline:
    """An explicit timestamp checked on the primary application page."""

    source_url: str
    source_excerpt: str
    raw_timestamp: str
    deadline_at: datetime

    def __post_init__(self) -> None:
        canonical_url(self.source_url)
        if not self.source_excerpt.strip() or self.raw_timestamp not in self.source_excerpt:
            raise ValueError("deadline timestamp must appear in application-page evidence")
        try:
            parsed = datetime.fromisoformat(self.raw_timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("deadline needs an explicit ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("deadline timestamp needs an explicit timezone")
        if self.deadline_at.tzinfo is not timezone.utc or parsed.astimezone(timezone.utc) != self.deadline_at:
            raise ValueError("deadline instant does not match source timestamp")


def sync_posting_deadline(db_path: Path, calendar: CalendarAdapter, *, opportunity_id: str,
                          deadline: VerifiedPostingDeadline, now: datetime) -> CalendarSyncResult:
    """Use the existing one-event-per-opportunity Calendar identity.

    A pending row is committed before the provider call. Changed dates require
    review rather than replacing an email-backed or prior page-backed event.
    """
    if now.tzinfo is not timezone.utc:
        raise ValueError("now must be UTC")
    validate_calendar_instant(deadline.deadline_at, now=now)
    if deadline.deadline_at <= now:
        raise DeadlineReviewRequired("application-page deadline is not in the future")
    db_path = Path(db_path)
    if not db_path.is_file():
        raise ValueError("Keeper database does not exist")
    event_id = stable_key("deadline", opportunity_id, "application-deadline")
    observed_url = canonical_url(deadline.source_url)
    evidence = f"{observed_url} | {deadline.source_excerpt.strip()}"
    with closing(sqlite3.connect(db_path, timeout=10)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        opportunity = conn.execute(
            "SELECT organization, title, canonical_url FROM opportunities WHERE id = ?", (opportunity_id,),
        ).fetchone()
        if opportunity is None:
            raise ValueError("opportunity does not exist")
        observed = conn.execute(
            "SELECT 1 FROM opportunity_observations WHERE opportunity_id = ? AND observed_url = ? LIMIT 1",
            (opportunity_id, observed_url),
        ).fetchone()
        if observed_url != opportunity[2] and observed is None:
            raise ValueError("deadline page does not match a recorded application URL")
        existing = conn.execute(
            "SELECT deadline_at, evidence, provider, provider_event_id FROM deadline_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        if existing is None:
            with conn:
                conn.execute(
                    """INSERT INTO deadline_events
                       (id, opportunity_id, deadline_at, evidence, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (event_id, opportunity_id, to_storage(deadline.deadline_at), evidence,
                     to_storage(now), to_storage(now)),
                )
            provider_id = None
        else:
            old_stamp, old_evidence, provider, provider_id = existing
            if provider not in (None, "google-calendar"):
                raise IdempotencyConflict("deadline belongs to another provider")
            if from_storage(old_stamp) != deadline.deadline_at:
                return CalendarSyncResult("review", event_id, provider_id,
                                          "changed application-page deadline needs review")
            if provider_id:
                with conn:
                    conn.execute(
                        "UPDATE opportunities SET deadline = ?, deadline_evidence = ? WHERE id = ?",
                        (to_storage(deadline.deadline_at), evidence, opportunity_id),
                    )
                return CalendarSyncResult("unchanged", event_id, provider_id,
                                          "deadline already scheduled")
            if old_evidence != evidence:
                return CalendarSyncResult("review", event_id, None,
                                          "pending deadline has different source evidence")
    summary = f"Application deadline: {opportunity[0]} — {opportunity[1]}"
    provider_id = calendar.upsert_deadline(
        event_id, deadline.deadline_at, summary, idempotency_key=event_id,
        provider_event_id=provider_id,
    )
    with closing(sqlite3.connect(db_path, timeout=10)) as conn, conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """UPDATE deadline_events SET provider = 'google-calendar', provider_event_id = ?,
               updated_at = ? WHERE id = ?""",
            (provider_id, to_storage(now), event_id),
        )
        conn.execute(
            "UPDATE opportunities SET deadline = ?, deadline_evidence = ? WHERE id = ?",
            (to_storage(deadline.deadline_at), evidence, opportunity_id),
        )
    return CalendarSyncResult("created", event_id, provider_id, "calendar event synchronized")
