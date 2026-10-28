"""Application tracker: confirmation, interview and decision emails move a tracked application.

The logic is keeper_core.application_messages (Rishi). It only acts when the email
names an organization and role Keeper already tracks; anything uncertain is review.
"""
from contextlib import closing

from keeper_core.application_messages import (
    detect_application_signal, process_application_email, resolve_email_application,
)
from keeper_core.contracts import TriageBucket

from .base import ALREADY_DONE, DONE, PLANNED, REVIEW, SKIPPED, Outcome, connect

NAME = "tracker"
_STATUS = {"updated": DONE, "reused": ALREADY_DONE, "ignored": SKIPPED, "review": REVIEW}


def wants(email, result):
    return result.career is True and result.bucket in (TriageBucket.ACTIONABLE, TriageBucket.DUE) \
        and not result.scam_signals


def run(email, result, ctx):
    if ctx.dry_run:
        detection = detect_application_signal(email)
        if detection.status != "detected":
            return Outcome(NAME, email.id, SKIPPED if detection.status == "none" else REVIEW, detection.reason)
        with closing(connect(ctx.db_path, read_only=True)) as conn:
            identity = resolve_email_application(conn, email)
        if identity.status != "matched":
            return Outcome(NAME, email.id, REVIEW, identity.reason)
        return Outcome(NAME, email.id, PLANNED, f"would record: {detection.signal.reason}")
    with closing(connect(ctx.db_path)) as conn, conn:
        processed = process_application_email(conn, email)
    status = _STATUS[processed.status]
    detail = processed.reason
    if status == DONE and processed.application is not None:
        detail += f"; application is now {processed.application.status.value}"
    return Outcome(NAME, email.id, status, detail)
