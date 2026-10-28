"""Calendar deadlines. Owner: Vi.

One calendar event per verified deadline (result.extracted_deadline, which
triage only sets when the exact timestamp is in the email). An extension moves
the existing event through CalendarAdapter.upsert_deadline instead of adding a
second one. Record each event in deadline_events.
"""
from keeper_core.contracts import TriageBucket

from .base import NOT_BUILT, Outcome

NAME = "deadlines"


def wants(email, result):
    return result.bucket is TriageBucket.DUE and result.extracted_deadline is not None \
        and not result.scam_signals


def run(email, result, ctx):
    return Outcome(NAME, email.id, NOT_BUILT, "calendar deadline not built yet (Vi)")
