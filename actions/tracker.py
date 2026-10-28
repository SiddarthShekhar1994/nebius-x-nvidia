"""Application tracker. Owner: Rishi.

Confirmation, interview and rejection emails move an application forward
through keeper_core.workflow.advance_application, once per email, and record
the email that caused each change (applications, application_status_changes,
application_source_emails).
"""
from keeper_core.contracts import TriageBucket

from .base import NOT_BUILT, Outcome

NAME = "tracker"


def wants(email, result):
    return result.career is True and result.bucket in (TriageBucket.ACTIONABLE, TriageBucket.DUE) \
        and not result.scam_signals


def run(email, result, ctx):
    return Outcome(NAME, email.id, NOT_BUILT, "tracker update not built yet (Rishi)")
