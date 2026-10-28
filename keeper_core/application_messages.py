"""Conservative, deterministic application email signals and tracker updates."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from .applications import (
    IdentityResolution, apply_message_transition, resolve_message_application,
)
from .contracts import Application, ApplicationStatus, EmailRecord
from .time import from_storage
from .workflow import TransitionConflict, normalize_identity


@dataclass(frozen=True, slots=True)
class ApplicationSignal:
    status: ApplicationStatus
    reason: str
    source_evidence: str
    decision_outcome: str | None = None


@dataclass(frozen=True, slots=True)
class SignalDetection:
    status: str  # detected, review, none
    signal: ApplicationSignal | None
    reason: str


@dataclass(frozen=True, slots=True)
class MessageProcessingResult:
    status: str  # updated, reused, review, ignored
    reason: str
    application: Application | None = None
    signal: ApplicationSignal | None = None
    identity: IdentityResolution | None = None


_SIGNALS = {
    "offered": (
        r"\b(?:pleased|delighted) to offer you\b(?!\s+(?:an?\s+)?interview\b)",
        r"\b(?:extend|make) (?:you )?an offer\b",
        r"(?<!not an )(?<!not a )\boffer of employment\b", r"\byou have been offered\b",
    ),
    "rejected": (
        r"\bnot selected\b", r"\b(?:will|are|have decided to|decided) not (?:be )?mov(?:e|ing) forward\b",
        r"\bapplication (?:was|is) unsuccessful\b",
        r"\bdecided to pursue other candidates\b",
    ),
    "withdrawn": (r"\byour application has been withdrawn\b",),
    "closed": (r"\b(?:position|role) has been filled\b", r"\byour application has been closed\b"),
    "interview": (
        r"\binterview invitation\b", r"\b(?:invite|inviting) you to (?:an )?interview\b",
        r"\binvited to (?:an )?interview\b", r"\b(?:would like|want) to interview you\b",
        r"\bschedule your interview\b", r"\bpleased to offer you an interview\b",
    ),
    "applied": (
        r"\bapplication (?:has been )?received\b", r"\breceived your application\b",
        r"\bthank you for (?:applying|your application)\b", r"\bsuccessfully submitted your application\b",
        r"\byour application (?:has been submitted|is complete)\b",
    ),
}
_UNSPECIFIED_DECISION = re.compile(
    r"\b(?:decision (?:regarding|about|on) your application|"
    r"(?:have|has) (?:made|reached) a decision (?:on|regarding|about) your application|"
    r"your application status has changed)\b",
    re.IGNORECASE,
)


def detect_application_signal(email: EmailRecord) -> SignalDetection:
    """Recognize explicit lifecycle phrases; conflicting decisions require review."""
    text = f"{email.subject}\n{email.body_excerpt}"
    found = {}
    for kind, patterns in _SIGNALS.items():
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                found[kind] = match.group(0)
                break
    decisions = [kind for kind in ("offered", "rejected", "withdrawn", "closed") if kind in found]
    if len(decisions) > 1:
        return SignalDetection("review", None, "message contains conflicting decision signals")
    if decisions:
        outcome = decisions[0]
        signal = ApplicationSignal(ApplicationStatus.DECISION, f"Decision: {outcome}",
                                   found[outcome], outcome)
    elif "interview" in found:
        signal = ApplicationSignal(ApplicationStatus.INTERVIEW, "Interview invitation", found["interview"])
    elif "applied" in found:
        signal = ApplicationSignal(ApplicationStatus.APPLIED, "Application confirmation", found["applied"])
    else:
        if _UNSPECIFIED_DECISION.search(text):
            return SignalDetection("review", None, "decision mentioned without a specific outcome")
        return SignalDetection("none", None, "no explicit application lifecycle signal")
    return SignalDetection("detected", signal, signal.reason)


def _identity_from_message(conn: sqlite3.Connection, email: EmailRecord) -> tuple[str | None, str | None]:
    """Use tracked names found in message text; a subject-only mention is insufficient."""
    subject = f" {normalize_identity(email.subject)} "
    body = f" {normalize_identity(email.body_excerpt)} "
    combined = subject + body
    matches = []
    for organization, title in conn.execute("SELECT organization, title FROM opportunities ORDER BY id"):
        org = f" {normalize_identity(organization)} "
        role = f" {normalize_identity(title)} "
        if org in combined and role in combined and (org in body or role in body):
            matches.append((organization, title))
    if len(matches) == 1:
        return matches[0]
    return None, None


def process_application_email(
    conn: sqlite3.Connection, email: EmailRecord, *,
    organization: str | None = None, title: str | None = None,
) -> MessageProcessingResult:
    """Apply a detected signal to one matched, already stored email and application.

    Callers may supply verified extracted names; otherwise both names must be
    found in the message against tracked opportunities. Uncertain signals or
    identity never mutate the application.
    """
    saved = conn.execute(
        """SELECT provider, provider_id, sender, subject, received_at, body_excerpt
           FROM email_records WHERE id = ?""", (email.id,),
    ).fetchone()
    if saved is None:
        return MessageProcessingResult("review", "source email is not stored")
    if (*saved[:4], from_storage(saved[4]), saved[5]) != (
        email.provider, email.provider_id, email.sender, email.subject,
        email.received_at, email.body_excerpt,
    ):
        return MessageProcessingResult("review", "stored email differs from supplied source")
    detection = detect_application_signal(email)
    if detection.status == "none":
        return MessageProcessingResult("ignored", detection.reason)
    if detection.status == "review":
        return MessageProcessingResult("review", detection.reason)
    if organization is None and title is None:
        organization, title = _identity_from_message(conn, email)
    identity = resolve_message_application(conn, organization=organization, title=title)
    if identity.status != "matched":
        return MessageProcessingResult("review", identity.reason, signal=detection.signal, identity=identity)
    before = conn.execute(
        "SELECT count(*) FROM application_status_changes WHERE application_id = ?",
        (identity.application_id,),
    ).fetchone()[0]
    signal = detection.signal
    try:
        _, application = apply_message_transition(
            conn, organization=organization, title=title, status=signal.status,
            changed_at=email.received_at, reason=signal.reason,
            source_evidence=signal.source_evidence, source_email_id=email.id,
            decision_outcome=signal.decision_outcome,
        )
    except TransitionConflict as exc:
        return MessageProcessingResult("review", str(exc), signal=signal, identity=identity)
    after = len(application.status_history)
    status = (
        "updated" if after > before else
        "reused" if email.id in application.source_email_ids else "ignored"
    )
    return MessageProcessingResult(status, signal.reason, application, signal, identity)
