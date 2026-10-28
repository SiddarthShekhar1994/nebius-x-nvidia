"""Reply drafts. Owner: Zayd.

One Gmail draft per actionable email, written by Super through the router and
saved with GmailAdapter.create_draft. Keeper never sends: there is no send call
anywhere, and drafts are recorded in draft_records with requires_review set.
"""
from keeper_core.contracts import TriageBucket

from .base import NOT_BUILT, Outcome

NAME = "drafts"


def wants(email, result):
    return result.bucket is TriageBucket.ACTIONABLE and not result.scam_signals


def run(email, result, ctx):
    return Outcome(NAME, email.id, NOT_BUILT, "reply draft not built yet (Zayd)")
