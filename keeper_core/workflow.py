"""Pure identity and application transition rules shared by integrations."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import replace
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .contracts import (
    Application, ApplicationStatus, ApplicationStatusChange, CorrectionRecord, ManualOverride,
    TriageBucket,
)


class TransitionConflict(ValueError):
    """Evidence contradicts the current application state or prior event."""


_ORDER = {
    ApplicationStatus.FOUND: 0,
    ApplicationStatus.APPLIED: 1,
    ApplicationStatus.INTERVIEW: 2,
    ApplicationStatus.DECISION: 3,
}


def stable_key(namespace: str, *parts: str) -> str:
    """Opaque deterministic key with unambiguous component boundaries."""
    if not namespace or not parts or any(not part for part in parts):
        raise ValueError("stable keys require a namespace and nonempty parts")
    payload = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{namespace}:{digest}"


def canonical_url(url: str) -> str:
    """Normalize a posting URL without dropping nontracking query parameters."""
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("opportunity URL must be an http(s) URL with a host")
    host = parsed.hostname.lower()
    port = parsed.port
    if port and (parsed.scheme.lower(), port) not in (("http", 80), ("https", 443)):
        host = f"{host}:{port}"
    path = parsed.path.rstrip("/") or "/"
    query = sorted(
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in ("fbclid", "gclid")
    )
    return urlunsplit((parsed.scheme.lower(), host, path, urlencode(query), ""))


def opportunity_key(url: str, organization: str, title: str) -> str:
    normalized_org = " ".join(organization.casefold().split())
    normalized_title = " ".join(title.casefold().split())
    return stable_key("opportunity", canonical_url(url), normalized_org, normalized_title)


def normalize_identity(value: str) -> str:
    """Normalize display names without guessing synonyms or abbreviations."""
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[^\W_]+", value, flags=re.UNICODE))


def organization_key(organization: str) -> str:
    normalized = normalize_identity(organization)
    if not normalized:
        raise ValueError("organization is required")
    return stable_key("organization", normalized)


def opportunity_identity_key(organization: str, title: str) -> str:
    normalized_title = normalize_identity(title)
    if not normalized_title:
        raise ValueError("title is required")
    return stable_key("opportunity-identity", organization_key(organization), normalized_title)


def resolve_triage_bucket(proposed: TriageBucket | None, career: bool | None) -> TriageBucket | None:
    """Career or unverified career mail cannot be put in junk automatically."""
    if proposed is TriageBucket.JUNK and career is not False:
        return TriageBucket.ACTIONABLE if career else None
    return proposed


def advance_application(
    application: Application,
    status: ApplicationStatus,
    *,
    changed_at: datetime,
    reason: str,
    source_email_id: str | None = None,
    correction: CorrectionRecord | None = None,
    decision_outcome: str | None = None,
    source_evidence: str | None = None,
) -> Application:
    """Apply one evidence-backed transition, returning the original on replay/stale mail.

    Automatic events use their source email as the event identity. A manual
    event needs an approved correction tied to this application.
    """
    if changed_at.tzinfo is not timezone.utc:
        raise ValueError("changed_at must be UTC")
    if not reason.strip():
        raise ValueError("transition needs a reason")
    if source_evidence is not None and not source_evidence.strip():
        raise ValueError("source evidence cannot be blank")
    manual = correction is not None
    if manual and source_email_id is not None:
        raise ValueError("manual transitions cannot use a source email")
    if not manual and not source_email_id:
        raise ValueError("automatic transitions need a source email ID")
    if correction is not None and (
        correction.approval_status != "approved"
        or correction.target_type != "application"
        or correction.target_id != application.id
        or not correction.actor_reference
        or correction.approved_at != changed_at
    ):
        raise ValueError("manual transition needs an approved correction for this application")
    event_key = f"correction:{correction.id}" if manual else f"email:{source_email_id}"
    change_id = stable_key("application-change", application.id, event_key)
    for old in application.status_history:
        if old.id == change_id:
            if old.status != status or (
                status is ApplicationStatus.DECISION
                and decision_outcome != old.decision_outcome
            ):
                raise TransitionConflict("same event proposes a different status or outcome")
            return application
    if not manual and source_email_id in application.source_email_ids:
        raise TransitionConflict("email was already attached with a different outcome")
    if changed_at < application.updated_at:
        if manual:
            raise TransitionConflict("correction approval predates current application state")
        return application  # older evidence never reverses a newer observation
    if not manual and application.manual_override is not None:
        return application  # a manual correction holds until another user action
    if not manual and _ORDER[status] < _ORDER[application.status]:
        raise TransitionConflict("automatic status regression")
    if not manual and application.status is ApplicationStatus.DECISION and status != application.status:
        raise TransitionConflict("decision is terminal for automatic transitions")
    if status is ApplicationStatus.DECISION and not decision_outcome:
        raise ValueError("decision transition needs a specific outcome")
    if status is not ApplicationStatus.DECISION and decision_outcome is not None:
        raise ValueError("decision outcome only belongs to decision status")
    if not manual and status is ApplicationStatus.DECISION and application.status is ApplicationStatus.DECISION:
        if decision_outcome != application.decision_outcome:
            raise TransitionConflict("automatic evidence contradicts the recorded decision")
    emails = application.source_email_ids
    if source_email_id and source_email_id not in emails:
        emails += (source_email_id,)
    change = ApplicationStatusChange(change_id, status, changed_at, source_email_id, reason, manual,
                                     source_evidence, correction.id if correction else None,
                                     decision_outcome)
    override = (
        ManualOverride(correction.actor_reference, correction.correction, changed_at)
        if correction else application.manual_override
    )
    return replace(
        application, status=status, status_history=application.status_history + (change,),
        source_email_ids=emails, updated_at=changed_at,
        manual_override=override,
        decision_outcome=decision_outcome,
    )
