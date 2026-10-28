"""Deterministic offline implementations of Keeper's five provider interfaces."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .adapters import (CalendarEventPage, CalendarEventSummary, EmailPage,
                       IdempotencyConflict, ModelAnswer, ProviderNotFound, ReadEmail, SearchHit)
from .contracts import EmailRecord, IngestionMetadata, ModelMetadata
from .time import from_storage


def _fixture_email(data: dict) -> EmailRecord:
    received_at = from_storage(data["received_at"])
    if received_at is None:
        raise ValueError("fixture email needs received_at")
    return EmailRecord(
        id=data["id"], provider=data["provider"], provider_id=data["provider_id"],
        thread_id=data.get("thread_id"), sender=data["sender"], subject=data["subject"],
        received_at=received_at, body_excerpt=data["body_excerpt"],
        labels=tuple(data.get("labels", ())), raw_link_reference=data.get("raw_link_reference"),
        ingestion=IngestionMetadata(
            ingested_at=received_at, run_id="fixture:run-1", source="demo-fixture"
        ),
    )


@dataclass
class FakeGmail:
    emails: tuple[EmailRecord, ...]
    labels_by_email: dict[str, set[str]] = field(default_factory=dict)
    drafts_by_key: dict[str, tuple[str, str, str, str]] = field(default_factory=dict)
    label_keys: dict[str, tuple[str, tuple[str, ...]]] = field(default_factory=dict)

    def check_health(self) -> None:
        pass

    def list_emails(self, *, cursor: str | None = None, limit: int = 50) -> EmailPage:
        if limit < 1:
            raise ValueError("limit must be positive")
        start = int(cursor) if cursor is not None else 0
        if start < 0 or start > len(self.emails):
            raise ValueError("invalid cursor")
        end = min(start + limit, len(self.emails))
        return EmailPage(self.emails[start:end], str(end) if end < len(self.emails) else None)

    def read_email(self, email_id: str) -> ReadEmail:
        for email in self.emails:
            if email.id == email_id:
                return ReadEmail(email, email.body_excerpt)
        raise ProviderNotFound(email_id)

    def add_labels(self, email_id: str, labels: tuple[str, ...], *, idempotency_key: str) -> None:
        if not any(email.id == email_id for email in self.emails):
            raise ProviderNotFound(email_id)
        old = self.label_keys.get(idempotency_key)
        if old and old != (email_id, labels):
            raise IdempotencyConflict("label key reused for another change")
        if idempotency_key not in self.label_keys:
            self.labels_by_email.setdefault(email_id, set()).update(labels)
            self.label_keys[idempotency_key] = (email_id, labels)

    def create_draft(self, email_id: str, subject: str, body: str, *, idempotency_key: str) -> str:
        if not any(email.id == email_id for email in self.emails):
            raise ProviderNotFound(email_id)
        existing = self.drafts_by_key.get(idempotency_key)
        if existing and existing[1:] != (email_id, subject, body):
            raise IdempotencyConflict("draft key reused for different content")
        if idempotency_key not in self.drafts_by_key:
            draft_id = f"fake-draft:{len(self.drafts_by_key) + 1}"
            self.drafts_by_key[idempotency_key] = (draft_id, email_id, subject, body)
        return self.drafts_by_key[idempotency_key][0]


@dataclass
class FakeCalendar:
    events_by_key: dict[str, tuple[str, str, datetime, str]] = field(default_factory=dict)

    def check_health(self) -> None:
        pass

    def search_events(self, query: str, *, cursor: str | None = None, limit: int = 50) -> CalendarEventPage:
        if not 1 <= limit <= 250:
            raise ValueError("limit must be between 1 and 250")
        matches = [CalendarEventSummary(value[0], value[3], value[2])
                   for value in self.events_by_key.values() if query.casefold() in value[3].casefold()]
        start = int(cursor) if cursor is not None else 0
        if start < 0 or start > len(matches):
            raise ValueError("invalid cursor")
        end = min(start + limit, len(matches))
        return CalendarEventPage(tuple(matches[start:end]), str(end) if end < len(matches) else None)

    def create_deadline(self, event_id: str, deadline_at: datetime, summary: str, *,
                        idempotency_key: str, review_policy=None) -> str:
        return self.upsert_deadline(event_id, deadline_at, summary, idempotency_key=idempotency_key,
                                    review_policy=review_policy)

    def upsert_deadline(
        self, event_id: str, deadline_at: datetime, summary: str, *,
        idempotency_key: str, provider_event_id: str | None = None, review_policy=None,
    ) -> str:
        if deadline_at.tzinfo is not timezone.utc:
            raise ValueError("deadline_at must be UTC")
        existing = self.events_by_key.get(idempotency_key)
        if existing and existing[1] != event_id:
            raise IdempotencyConflict("event key reused for another Keeper event")
        assigned = existing[0] if existing else (provider_event_id or f"fake-event:{len(self.events_by_key) + 1}")
        if provider_event_id and provider_event_id != assigned:
            raise ValueError("provider event mapping changed")
        self.events_by_key[idempotency_key] = (assigned, event_id, deadline_at, summary)
        return assigned


@dataclass
class FakeTelegram:
    messages_by_key: dict[str, tuple[str, str, str]] = field(default_factory=dict)

    def check_health(self) -> None:
        pass

    def send_message(self, recipient_reference: str, text: str, *, idempotency_key: str) -> str:
        existing = self.messages_by_key.get(idempotency_key)
        if existing and existing[1:] != (recipient_reference, text):
            raise IdempotencyConflict("notification key reused for different content")
        if idempotency_key not in self.messages_by_key:
            message_id = f"fake-message:{len(self.messages_by_key) + 1}"
            self.messages_by_key[idempotency_key] = (message_id, recipient_reference, text)
        return self.messages_by_key[idempotency_key][0]


@dataclass
class FakeTavily:
    results_by_query: dict[str, tuple[SearchHit, ...]]

    def check_health(self) -> None:
        pass

    def search(self, query: str, *, limit: int = 5) -> tuple[SearchHit, ...]:
        if not query.strip() or not 1 <= limit <= 20:
            raise ValueError("query and limit between 1 and 20 are required")
        return self.results_by_query.get(query, ())[:limit]


@dataclass
class FakeModel:
    answers_by_task: dict[str, ModelAnswer]

    def complete(self, task_type: str, prompt: str) -> ModelAnswer:
        try:
            return self.answers_by_task[task_type]
        except KeyError:
            raise ProviderNotFound(f"no fixture answer for {task_type}") from None


def load_fakes(path: Path) -> tuple[FakeGmail, FakeCalendar, FakeTelegram, FakeTavily, FakeModel]:
    """Load stable, synthetic responses from a JSON fixture file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    gmail = FakeGmail(tuple(_fixture_email(item) for item in data["emails"]))
    tavily = FakeTavily({
        query: tuple(SearchHit(**hit) for hit in hits)
        for query, hits in data["search_results"].items()
    })
    model = FakeModel({
        task: ModelAnswer(
            content=answer["content"], metadata=ModelMetadata(**answer["metadata"])
        ) for task, answer in data["model_answers"].items()
    })
    return gmail, FakeCalendar(), FakeTelegram(), tavily, model
