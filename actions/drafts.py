"""Reply drafts: one unsent Gmail draft per actionable email, written by Super. Never sent.

The logic is keeper_core.reply_drafts (Rishi). Without a linked tracked application, or for an
unverified sender or a sensitive request, the draft is a short holding reply flagged for review.
"""
from contextlib import closing

from keeper_core.contracts import TriageBucket
from keeper_core.reply_drafts import create_reply_draft

from .base import ALREADY_DONE, DONE, PLANNED, SKIPPED, Outcome, connect, linked_application

NAME = "drafts"


def wants(email, result):
    return result.bucket is TriageBucket.ACTIONABLE and not result.scam_signals


def run(email, result, ctx):
    if email.provider != "gmail":
        return Outcome(NAME, email.id, SKIPPED, "drafts are only saved for Gmail messages")
    with closing(connect(ctx.db_path, read_only=True)) as conn:
        linked = linked_application(conn, email.id)
    if ctx.dry_run:
        note = "" if linked else ", flagged for review (no tracked application)"
        return Outcome(NAME, email.id, PLANNED, "would save an unsent reply draft" + note)
    draft = create_reply_draft(ctx.db_path, ctx.gmail, ctx.model, email.id, now=ctx.now(),
                               application_id=linked[0] if linked else None)
    detail = "unsent reply draft saved"
    if draft.review_flags:
        detail += " for review: " + ", ".join(draft.review_flags)
    return Outcome(NAME, email.id, ALREADY_DONE if draft.reused else DONE, detail, draft.provider_draft_id)
