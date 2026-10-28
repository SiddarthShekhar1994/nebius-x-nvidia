"""Provider boundaries. Live implementations may use any client library behind these APIs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .contracts import EmailRecord, ModelMetadata
from .deadlines import DeadlineReviewPolicy


class AdapterError(Exception):
    """Provider operation failed; subclasses describe recovery behavior."""


class AuthenticationError(AdapterError):
    """Credentials or scopes need operator attention; do not retry blindly."""


class RateLimited(AdapterError):
    def __init__(self, message: str, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ProviderUnavailable(AdapterError):
    """Transient provider or network failure; retry with a bounded backoff."""


class ProviderNotFound(AdapterError):
    """Referenced provider object does not exist."""


class InvalidProviderResponse(AdapterError):
    """Provider or model response cannot be parsed into the agreed contract."""


class IdempotencyConflict(AdapterError):
    """One retry key was reused for a different target or payload."""


@dataclass(frozen=True, slots=True)
class EmailPage:
    records: tuple[EmailRecord, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ReadEmail:
    record: EmailRecord
    body_text: str
    message_id_header: str | None = None
    references_header: str | None = None


@dataclass(frozen=True, slots=True)
class CalendarEventSummary:
    provider_event_id: str
    summary: str
    starts_at: datetime | None


@dataclass(frozen=True, slots=True)
class CalendarEventPage:
    events: tuple[CalendarEventSummary, ...]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class SearchHit:
    url: str
    title: str
    snippet: str
    source: str


@dataclass(frozen=True, slots=True)
class ModelAnswer:
    content: str
    metadata: ModelMetadata


class GmailAdapter(Protocol):
    def check_health(self) -> None: ...
    def list_emails(self, *, cursor: str | None = None, limit: int = 50) -> EmailPage: ...
    def read_email(self, email_id: str) -> ReadEmail: ...
    def add_labels(self, email_id: str, labels: tuple[str, ...], *, idempotency_key: str) -> None: ...
    def create_draft(self, email_id: str, subject: str, body: str, *, idempotency_key: str) -> str: ...


class CalendarAdapter(Protocol):
    def check_health(self) -> None: ...
    def search_events(self, query: str, *, cursor: str | None = None, limit: int = 50) -> CalendarEventPage: ...
    def create_deadline(
        self, event_id: str, deadline_at: datetime, summary: str, *, idempotency_key: str,
        review_policy: DeadlineReviewPolicy | None = None,
    ) -> str: ...
    def upsert_deadline(
        self, event_id: str, deadline_at: datetime, summary: str, *,
        idempotency_key: str, provider_event_id: str | None = None,
        review_policy: DeadlineReviewPolicy | None = None,
    ) -> str: ...


class TelegramAdapter(Protocol):
    def check_health(self) -> None: ...
    def send_message(self, recipient_reference: str, text: str, *, idempotency_key: str) -> str: ...


class TavilyAdapter(Protocol):
    def check_health(self) -> None: ...
    def search(self, query: str, *, limit: int = 5) -> tuple[SearchHit, ...]: ...


class ModelAdapter(Protocol):
    def complete(self, task_type: str, prompt: str) -> ModelAnswer: ...
