"""Google Calendar deadline adapter with durable Keeper-to-provider event mapping."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from contextlib import closing
from pathlib import Path
from typing import Callable

from keeper_core.adapters import (
    AdapterError, AuthenticationError, CalendarEventPage, CalendarEventSummary,
    IdempotencyConflict, InvalidProviderResponse, ProviderNotFound,
    ProviderUnavailable, RateLimited,
)
from keeper_core.deadlines import DeadlineReviewPolicy, validate_calendar_instant
from keeper_core.time import to_storage

_SCOPE = "https://www.googleapis.com/auth/calendar.events"
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
_RETRY_STATUSES = {500, 502, 503, 504}


def _event_key(event_id: str) -> str:
    return hashlib.sha256(event_id.encode("utf-8")).hexdigest()


def _provider_id(event_id: str) -> str:
    # Calendar allows caller-supplied event IDs using base32hex characters.
    return "keeper" + _event_key(event_id)


def _status(error: Exception) -> int | None:
    return getattr(getattr(error, "resp", None), "status", None)


def _rate_reason(error: Exception) -> bool:
    try:
        data = json.loads(getattr(error, "content", b"{}"))
        return any(entry.get("reason") in _RATE_REASONS
                   for entry in data.get("error", {}).get("errors", []))
    except (ValueError, TypeError, AttributeError):
        return False


def _retry_after(error: Exception) -> float | None:
    resp = getattr(error, "resp", None)
    header = resp.get("retry-after") if resp is not None and hasattr(resp, "get") else None
    try:
        return max(0.0, min(30.0, float(header))) if header is not None else None
    except (ValueError, TypeError):
        return None


def _start(value: dict) -> datetime | None:
    start = value.get("start", {})
    if not isinstance(start, dict):
        raise InvalidProviderResponse("Calendar event has invalid start")
    instant = start.get("dateTime")
    if instant is None:
        return None  # An all-day event has no known instant in this contract.
    if not isinstance(instant, str):
        raise InvalidProviderResponse("Calendar event has invalid start time")
    try:
        parsed = datetime.fromisoformat(instant.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise InvalidProviderResponse("Calendar event has invalid start time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidProviderResponse("Calendar event start lacks a timezone")
    return parsed.astimezone(timezone.utc)


class LiveCalendar:
    """One calendar and one Keeper database; deadline rows must exist first."""

    def __init__(
        self, service: object, db_path: Path, *, calendar_id: str = "primary",
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        max_attempts: int = 3,
    ):
        if not calendar_id or max_attempts < 1:
            raise ValueError("calendar_id and positive max_attempts are required")
        self._service = service
        self._db_path = Path(db_path)
        self._calendar_id = calendar_id
        self._sleep = sleep
        self._now = now
        self._max_attempts = max_attempts
        self._keys: dict[str, str] = {}

    @classmethod
    def from_token_file(
        cls, token_file: Path, db_path: Path, *, calendar_id: str = "primary",
    ) -> "LiveCalendar":
        try:
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise RuntimeError("install google-api-python-client and google-auth-oauthlib") from exc
        credentials = Credentials.from_authorized_user_file(str(token_file))
        if _SCOPE not in (credentials.scopes or []):
            raise AuthenticationError("runtime token lacks calendar.events")
        return cls(build("calendar", "v3", credentials=credentials, cache_discovery=False),
                   db_path, calendar_id=calendar_id)

    def check_health(self) -> None:
        """Read one event page without creating or changing an event."""
        page = self._execute(lambda: self._service.events().list(
            calendarId=self._calendar_id, maxResults=1))
        if not isinstance(page.get("items", []), list):
            raise InvalidProviderResponse("Calendar health response has invalid events")

    def _execute(self, request: Callable[[], object], *, safe_to_retry: bool = True) -> dict:
        for attempt in range(self._max_attempts):
            try:
                result = request().execute()
                if not isinstance(result, dict):
                    raise InvalidProviderResponse("Calendar response is not an object")
                return result
            except (OSError, TimeoutError) as exc:
                if safe_to_retry and attempt + 1 < self._max_attempts:
                    self._sleep(min(2 ** attempt, 30))
                    continue
                raise ProviderUnavailable("Calendar transport failed") from exc
            except Exception as exc:
                if isinstance(exc, AdapterError):
                    raise
                status = _status(exc)
                if status is None:
                    raise
                rate = status == 429 or (status == 403 and _rate_reason(exc))
                delay = _retry_after(exc)
                if (rate or status in _RETRY_STATUSES) and safe_to_retry and attempt + 1 < self._max_attempts:
                    self._sleep(delay if delay is not None else min(2 ** attempt, 30))
                    continue
                if rate:
                    raise RateLimited("Calendar rate limit reached", delay) from exc
                if status in (401, 403):
                    raise AuthenticationError("Calendar authorization failed") from exc
                if status == 404:
                    raise ProviderNotFound("Calendar event not found") from exc
                if status in _RETRY_STATUSES:
                    raise ProviderUnavailable("Calendar is unavailable") from exc
                raise AdapterError(f"Calendar request failed (HTTP {status})") from exc
        raise AssertionError("unreachable retry state")

    def search_events(self, query: str, *, cursor: str | None = None, limit: int = 50) -> CalendarEventPage:
        if not query.strip() or not 1 <= limit <= 250:
            raise ValueError("query and limit between 1 and 250 are required")
        params = {"calendarId": self._calendar_id, "q": query, "maxResults": limit}
        if cursor is not None:
            params["pageToken"] = cursor
        page = self._execute(lambda: self._service.events().list(**params))
        items = page.get("items", [])
        if not isinstance(items, list) or any(not isinstance(item, dict) or
                                              not isinstance(item.get("id"), str) or not item["id"] or
                                              not isinstance(item.get("summary", ""), str)
                                              for item in items):
            raise InvalidProviderResponse("Calendar search returned invalid events")
        next_cursor = page.get("nextPageToken")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise InvalidProviderResponse("Calendar search returned invalid page token")
        return CalendarEventPage(tuple(CalendarEventSummary(item["id"], item.get("summary", ""), _start(item))
                                       for item in items), next_cursor)

    def _stored_id(self, event_id: str) -> str | None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            row = conn.execute("SELECT provider, provider_event_id FROM deadline_events WHERE id = ?",
                               (event_id,)).fetchone()
        if row is None:
            raise ProviderNotFound(f"Keeper deadline row {event_id!r} does not exist")
        if row[0] not in (None, "google-calendar"):
            raise IdempotencyConflict("deadline belongs to another calendar provider")
        return row[1]

    def _save_mapping(self, event_id: str, provider_id: str, deadline_at: datetime) -> None:
        with closing(sqlite3.connect(self._db_path, timeout=10)) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT provider, provider_event_id FROM deadline_events WHERE id = ?",
                               (event_id,)).fetchone()
            if row is None:
                raise ProviderNotFound(f"Keeper deadline row {event_id!r} disappeared")
            if row[0] not in (None, "google-calendar") or (row[1] is not None and row[1] != provider_id):
                raise IdempotencyConflict("provider event mapping changed concurrently")
            conn.execute("""UPDATE deadline_events SET provider = 'google-calendar', provider_event_id = ?,
                           deadline_at = ?, updated_at = ? WHERE id = ?""",
                         (provider_id, to_storage(deadline_at), to_storage(self._now()), event_id))

    def _find_by_key(self, key: str) -> str | None:
        cursor = None
        found = None
        for _ in range(50):
            params = {"calendarId": self._calendar_id,
                      "privateExtendedProperty": f"keeperDeadlineKey={key}", "maxResults": 250}
            if cursor:
                params["pageToken"] = cursor
            page = self._execute(lambda: self._service.events().list(**params))
            items = page.get("items", [])
            if not isinstance(items, list):
                raise InvalidProviderResponse("Calendar key search returned invalid events")
            for item in items:
                candidate = item.get("id") if isinstance(item, dict) else None
                if not isinstance(candidate, str) or not candidate:
                    raise InvalidProviderResponse("Calendar key search returned invalid event ID")
                if found is not None and found != candidate:
                    raise IdempotencyConflict("multiple Calendar events share the Keeper key")
                found = candidate
            cursor = page.get("nextPageToken")
            if cursor is not None and not isinstance(cursor, str):
                raise InvalidProviderResponse("Calendar key search returned invalid page token")
            if not cursor:
                return found
        raise ProviderUnavailable("Calendar key search exceeded 50 pages")

    def create_deadline(
        self, event_id: str, deadline_at: datetime, summary: str, *, idempotency_key: str,
        review_policy: DeadlineReviewPolicy | None = None,
    ) -> str:
        return self.upsert_deadline(event_id, deadline_at, summary,
                                    idempotency_key=idempotency_key, review_policy=review_policy)

    def upsert_deadline(
        self, event_id: str, deadline_at: datetime, summary: str, *,
        idempotency_key: str, provider_event_id: str | None = None,
        review_policy: DeadlineReviewPolicy | None = None,
    ) -> str:
        if not event_id or not idempotency_key or not summary.strip():
            raise ValueError("event ID, idempotency key, and summary are required")
        if deadline_at.tzinfo is None or deadline_at.utcoffset() is None:
            raise ValueError("deadline must have a timezone")
        deadline_at = validate_calendar_instant(deadline_at, now=self._now(), policy=review_policy)
        prior = self._keys.get(idempotency_key)
        if prior is not None and prior != event_id:
            raise IdempotencyConflict("calendar key reused for another deadline")
        stored_id = self._stored_id(event_id)
        if stored_id is not None and provider_event_id is not None and stored_id != provider_event_id:
            raise IdempotencyConflict("caller provider ID disagrees with stored mapping")
        key = _event_key(event_id)
        candidate = stored_id or provider_event_id or self._find_by_key(key)
        start = deadline_at.isoformat().replace("+00:00", "Z")
        end = (deadline_at + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
        body = {"summary": summary, "start": {"dateTime": start}, "end": {"dateTime": end},
                "extendedProperties": {"private": {"keeperDeadlineKey": key}}}
        if candidate:
            existing = self._execute(lambda: self._service.events().get(
                calendarId=self._calendar_id, eventId=candidate))
            if existing.get("extendedProperties", {}).get("private", {}).get("keeperDeadlineKey") != key:
                raise IdempotencyConflict("provider event ID belongs to another deadline")
            if existing.get("summary") != summary or _start(existing) != deadline_at:
                self._execute(lambda: self._service.events().patch(
                    calendarId=self._calendar_id, eventId=candidate, body=body, sendUpdates="none"))
            provider_id = candidate
        else:
            body["id"] = _provider_id(event_id)
            try:
                created = self._execute(lambda: self._service.events().insert(
                    calendarId=self._calendar_id, body=body, sendUpdates="none"), safe_to_retry=False)
                provider_id = created.get("id")
            except AdapterError as exc:
                if _status(exc.__cause__) != 409:
                    raise
                provider_id = body["id"]
                recovered = self._execute(lambda: self._service.events().get(
                    calendarId=self._calendar_id, eventId=provider_id))
                if recovered.get("extendedProperties", {}).get("private", {}).get("keeperDeadlineKey") != key:
                    raise IdempotencyConflict("provider event ID collision")
                if recovered.get("summary") != summary or _start(recovered) != deadline_at:
                    self._execute(lambda: self._service.events().patch(
                        calendarId=self._calendar_id, eventId=provider_id,
                        body=body, sendUpdates="none"))
            if not isinstance(provider_id, str) or not provider_id:
                raise InvalidProviderResponse("Calendar creation returned no event ID")
            if provider_id != body["id"]:
                raise InvalidProviderResponse("Calendar returned an unexpected event ID")
        self._save_mapping(event_id, provider_id, deadline_at)
        self._keys[idempotency_key] = event_id
        return provider_id
