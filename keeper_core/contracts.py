"""Network and storage independent records shared by Keeper's product lanes.

IDs are opaque, stable strings assigned by producers. Timestamps are aware UTC
datetimes; adapters convert provider timestamps before constructing records.
Unknown facts are None, rather than guessed values.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import StrEnum


class TriageBucket(StrEnum):
    JUNK = "junk"
    ACTIONABLE = "actionable"
    DUE = "due"
    NEWSLETTER = "newsletter"


class ApplicationStatus(StrEnum):
    FOUND = "found"
    APPLIED = "applied"
    INTERVIEW = "interview"
    DECISION = "decision"


class Eligibility(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNVERIFIED = "unverified"


def _check_utc(record: object) -> None:
    for field in fields(record):
        value = getattr(record, field.name)
        if isinstance(value, datetime) and value.tzinfo is not timezone.utc:
            raise ValueError(f"{field.name} must be a UTC datetime")


@dataclass(frozen=True, slots=True)
class IngestionMetadata:
    ingested_at: datetime
    run_id: str
    source: str  # e.g. gmail-inbox, demo-fixture

    def __post_init__(self) -> None:
        _check_utc(self)


@dataclass(frozen=True, slots=True)
class EmailRecord:
    id: str
    provider: str
    provider_id: str
    thread_id: str | None
    sender: str
    subject: str
    received_at: datetime
    body_excerpt: str
    labels: tuple[str, ...]
    raw_link_reference: str | None
    ingestion: IngestionMetadata

    def __post_init__(self) -> None:
        _check_utc(self)


@dataclass(frozen=True, slots=True)
class ModelMetadata:
    requested_model: str
    served_model: str | None
    prompt_version: str
    router_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class TriageResult:
    id: str
    email_id: str
    bucket: TriageBucket | None  # None means review / classification unavailable
    reason: str
    career: bool | None
    scam_signals: tuple[str, ...]
    extracted_deadline: datetime | None
    confidence: float | None  # model-provided, not calibrated
    model: ModelMetadata | None
    classified_at: datetime
    deadline_evidence: str | None = None

    def __post_init__(self) -> None:
        _check_utc(self)
        if self.confidence is not None and not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class Opportunity:
    id: str
    dedupe_key: str
    canonical_url: str
    organization: str
    title: str
    source: str
    discovered_at: datetime
    score: int | None  # 1–10; None when not scored
    explanation: str | None
    eligibility: Eligibility
    eligibility_reason: str | None
    deadline: datetime | None
    deadline_evidence: str | None = None

    def __post_init__(self) -> None:
        _check_utc(self)
        if self.score is not None and not 1 <= self.score <= 10:
            raise ValueError("score must be between 1 and 10")


@dataclass(frozen=True, slots=True)
class ApplicationStatusChange:
    id: str
    status: ApplicationStatus
    changed_at: datetime
    source_email_id: str | None
    reason: str
    manual: bool = False
    source_evidence: str | None = None
    correction_id: str | None = None
    decision_outcome: str | None = None

    def __post_init__(self) -> None:
        _check_utc(self)


@dataclass(frozen=True, slots=True)
class ManualOverride:
    actor_reference: str
    reason: str
    applied_at: datetime

    def __post_init__(self) -> None:
        _check_utc(self)


@dataclass(frozen=True, slots=True)
class Application:
    id: str
    opportunity_id: str
    status: ApplicationStatus
    status_history: tuple[ApplicationStatusChange, ...]
    source_email_ids: tuple[str, ...]
    created_at: datetime
    updated_at: datetime
    manual_override: ManualOverride | None = None
    decision_outcome: str | None = None

    def __post_init__(self) -> None:
        _check_utc(self)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at precedes created_at")
        if self.status_history and self.status_history[-1].status != self.status:
            raise ValueError("status must match the last history entry")


@dataclass(frozen=True, slots=True)
class DeadlineEvent:
    id: str
    opportunity_id: str
    application_id: str | None
    source_email_id: str | None
    deadline_at: datetime
    evidence: str
    provider: str | None
    provider_event_id: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        _check_utc(self)


@dataclass(frozen=True, slots=True)
class DraftRecord:
    id: str
    email_id: str
    application_id: str | None
    provider: str
    provider_draft_id: str | None
    subject: str
    body: str
    created_at: datetime
    updated_at: datetime
    requires_review: bool = True

    def __post_init__(self) -> None:
        _check_utc(self)


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    id: str
    channel: str
    recipient_reference: str
    purpose: str
    idempotency_key: str
    status: str  # pending, delivered, failed
    created_at: datetime
    delivered_at: datetime | None = None
    provider_message_id: str | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        _check_utc(self)


@dataclass(frozen=True, slots=True)
class SecurityEvent:
    id: str
    occurred_at: datetime
    category: str  # injection_attempt, network_denial, policy_error, etc.
    outcome: str
    evidence_reference: str
    source_email_id: str | None = None
    attempted_destination: str | None = None

    def __post_init__(self) -> None:
        _check_utc(self)


@dataclass(frozen=True, slots=True)
class CorrectionRecord:
    id: str
    target_type: str
    target_id: str
    correction: str
    created_at: datetime
    approval_status: str  # proposed, approved, rejected
    proposed_skill_name: str | None = None
    approved_at: datetime | None = None
    actor_reference: str | None = None

    def __post_init__(self) -> None:
        _check_utc(self)
