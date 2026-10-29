"""Calendar deadlines: one event per verified deadline; an extension moves the existing event.

The logic is keeper_core.deadlines and keeper_core.calendar_deadlines (Rishi). Keeper only
schedules one explicit, future deadline from an email that names a tracked opportunity.
"""
from contextlib import closing

from keeper_core.application_messages import resolve_email_application
from keeper_core.calendar_deadlines import sync_deadline_candidate
from keeper_core.contracts import TriageBucket
from keeper_core.deadlines import DeadlineReviewRequired, extract_deadline_candidates, validated_deadline_at
from keeper_core.time import to_storage

from .base import ALREADY_DONE, DONE, PLANNED, REVIEW, SKIPPED, Outcome, connect

NAME = "deadlines"
_STATUS = {"created": DONE, "updated": DONE, "unchanged": ALREADY_DONE, "review": REVIEW}


def wants(email, result):
    return result.bucket is TriageBucket.DUE and result.extracted_deadline is not None \
        and not result.scam_signals


def run(email, result, ctx):
    now = ctx.now()
    candidates = extract_deadline_candidates(email, result, now=now)
    if not candidates:
        return Outcome(NAME, email.id, SKIPPED, "no deadline phrase with a date")
    if len(candidates) > 1:
        return Outcome(NAME, email.id, REVIEW, f"{len(candidates)} possible deadlines in one email")
    candidate = candidates[0]
    try:
        deadline_at = validated_deadline_at(candidate, now=now)
    except DeadlineReviewRequired:
        return Outcome(NAME, email.id, REVIEW, "deadline needs review: " + ", ".join(candidate.flags or ("unclear date",)))
    when = to_storage(deadline_at)
    with closing(connect(ctx.db_path, read_only=True)) as conn:
        identity = resolve_email_application(conn, email)
        row = conn.execute("SELECT opportunity_id FROM applications WHERE id = ?",
                           (identity.application_id,)).fetchone() if identity.status == "matched" else None
    if row is None:
        return Outcome(NAME, email.id, REVIEW,
                       f"deadline {when} found, but the email doesn't name one tracked opportunity ({identity.reason})")
    if ctx.dry_run:
        return Outcome(NAME, email.id, PLANNED, f"would schedule {when}")
    synced = sync_deadline_candidate(ctx.db_path, ctx.calendar, candidate, opportunity_id=row[0],
                                     application_id=identity.application_id, now=now)
    status = _STATUS[synced.status]
    detail = synced.reason if status == REVIEW else f"deadline {synced.status}: {when}"
    return Outcome(NAME, email.id, status, detail, synced.provider_event_id)
