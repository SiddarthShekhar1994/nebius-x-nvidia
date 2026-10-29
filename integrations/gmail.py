"""Gmail inbox, label, and unsent-draft adapter.

The only Gmail mutations here are messages.modify, labels.create, and
drafts.create. The service object stays private; no send operation is exposed.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
from html.parser import HTMLParser
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path
from typing import Callable

from keeper_core.adapters import (
    AdapterError, AuthenticationError, EmailPage, IdempotencyConflict,
    InvalidProviderResponse, ProviderNotFound, ProviderUnavailable, RateLimited,
    ReadEmail,
)
from keeper_core.contracts import EmailRecord, IngestionMetadata

_GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
_RETRY_STATUSES = {500, 502, 503, 504}


def _provider_id(email_id: str) -> str:
    if not email_id.startswith("gmail:") or len(email_id) <= len("gmail:"):
        raise ValueError("email_id must be a gmail:<message-id> Keeper ID")
    return email_id[len("gmail:"):]


def _header(message: dict, name: str) -> str | None:
    for item in message.get("payload", {}).get("headers", []):
        if item.get("name", "").lower() == name.lower():
            return item.get("value")
    return None


def _plain_body(part: dict) -> str | None:
    if part.get("filename"):
        return None
    for child in part.get("parts", []):
        content = _plain_body(child)
        if content is not None:
            return content
    if part.get("mimeType") != "text/plain":
        return None
    encoded = part.get("body", {}).get("data")
    if not encoded:
        return None
    try:
        binary = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, binascii.Error) as exc:
        raise InvalidProviderResponse("Gmail returned invalid message body encoding") from exc
    content_type = next(
        (entry.get("value", "") for entry in part.get("headers", [])
         if entry.get("name", "").lower() == "content-type"),
        "",
    )
    charset = "utf-8"
    if "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=", 1)[1].split(";", 1)[0].strip(' "')
    try:
        return binary.decode(charset, errors="replace")
    except LookupError as exc:
        raise InvalidProviderResponse("Gmail returned unknown message charset") from exc


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("p", "br", "div"):
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def _html_body(part):
    if part.get("filename"):
        return None
    for child in part.get("parts", []):
        result = _html_body(child)
        if result is not None:
            return result
    if part.get("mimeType") == "text/html":
        text = _plain_body({**part, "mimeType": "text/plain"})
        if text is not None:
            parser = _HTMLText()
            parser.feed(text)
            return "".join(parser.text).strip()
    return None


def _http_status(exc: Exception) -> int | None:
    return getattr(getattr(exc, "resp", None), "status", None)


def _rate_reason(exc: Exception) -> bool:
    try:
        data = json.loads(getattr(exc, "content", b"{}"))
        errors = data.get("error", {}).get("errors", [])
        return any(item.get("reason") in _RATE_REASONS for item in errors)
    except (TypeError, ValueError, AttributeError):
        return False


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "resp", None)
    header = response.get("retry-after") if response and hasattr(response, "get") else None
    try:
        return max(0.0, min(float(header), 30.0)) if header is not None else None
    except (TypeError, ValueError):
        return None


class LiveGmail:
    """A narrow Gmail capability surface for a single demo account."""

    def __init__(
        self, service: object, *, run_id: str,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        max_attempts: int = 3,
    ):
        if not run_id or max_attempts < 1:
            raise ValueError("run_id and positive max_attempts are required")
        self._service = service
        self._run_id = run_id
        self._sleep = sleep
        self._now = now
        self._max_attempts = max_attempts
        self._label_keys: dict[str, tuple[str, tuple[str, ...]]] = {}

    @classmethod
    def from_token_file(cls, token_file: Path, *, run_id: str) -> "LiveGmail":
        """Build from the private runtime token over Gmail's REST API (see google_rest)."""
        from .google_rest import GMAIL_URL, AccessToken, GoogleService
        token = AccessToken(token_file)
        if _GMAIL_SCOPE not in token.scopes:
            raise AuthenticationError("runtime token lacks gmail.modify")
        return cls(GoogleService(GMAIL_URL, token), run_id=run_id)

    def check_health(self) -> None:
        """Read-only API and authorization probe; does not inspect inbox content."""
        profile = self._execute(lambda: self._service.users().getProfile(userId="me"))
        if not isinstance(profile.get("emailAddress"), str) or not profile["emailAddress"]:
            raise InvalidProviderResponse("Gmail profile has no email address")

    def verify_account(self, expected: str) -> None:
        """Require the operator's disposable demo account before reading its seed."""
        profile = self._execute(lambda: self._service.users().getProfile(userId="me"))
        actual = profile.get("emailAddress")
        if not isinstance(actual, str) or actual.casefold() != expected.casefold():
            raise AuthenticationError("Authenticated Gmail account does not match the demo account")

    def _execute(self, request: Callable[[], object], *, safe_to_retry: bool = True) -> dict:
        for attempt in range(self._max_attempts):
            try:
                response = request().execute()
                if not isinstance(response, dict):
                    raise InvalidProviderResponse("Gmail response is not an object")
                return response
            except (OSError, TimeoutError) as exc:
                if safe_to_retry and attempt + 1 < self._max_attempts:
                    self._sleep(min(2 ** attempt, 30))
                    continue
                raise ProviderUnavailable("Gmail transport failed") from exc
            except Exception as exc:
                if isinstance(exc, AdapterError):
                    raise
                status = _http_status(exc)
                if status is None:
                    raise
                rate_limited = status == 429 or (status == 403 and _rate_reason(exc))
                retry_after = _retry_after(exc)
                if (rate_limited or status in _RETRY_STATUSES) and safe_to_retry and attempt + 1 < self._max_attempts:
                    self._sleep(retry_after if retry_after is not None else min(2 ** attempt, 30))
                    continue
                if rate_limited:
                    raise RateLimited("Gmail rate limit reached", retry_after) from exc
                if status in (401, 403):
                    raise AuthenticationError("Gmail authorization failed") from exc
                if status == 404:
                    raise ProviderNotFound("Gmail object not found") from exc
                if status in _RETRY_STATUSES:
                    raise ProviderUnavailable("Gmail is unavailable") from exc
                raise AdapterError(f"Gmail request failed (HTTP {status})") from exc
        raise AssertionError("unreachable retry state")

    def list_message_ids(self, *, cursor: str | None = None, limit: int = 50):
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        params = {"userId": "me", "labelIds": ["INBOX"], "maxResults": limit}
        if cursor is not None:
            params["pageToken"] = cursor
        page = self._execute(lambda: self._service.users().messages().list(**params))
        refs = page.get("messages", [])
        if not isinstance(refs, list):
            raise InvalidProviderResponse("Gmail message page has no valid messages list")
        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
               for item in refs):
            raise InvalidProviderResponse("Gmail message page contains an invalid ID")
        next_cursor = page.get("nextPageToken")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise InvalidProviderResponse("Gmail returned an invalid page token")
        return tuple(item['id'] for item in refs), next_cursor

    def list_emails(self, *, cursor: str | None = None, limit: int = 50) -> EmailPage:
        ids, next_cursor = self.list_message_ids(cursor=cursor, limit=limit)
        return EmailPage(tuple(self.read_email(f"gmail:{item}").record for item in ids), next_cursor)

    def read_email(self, email_id: str) -> ReadEmail:
        provider_id = _provider_id(email_id)
        message = self._execute(
            lambda: self._service.users().messages().get(userId="me", id=provider_id, format="full")
        )
        if message.get("id") != provider_id or "internalDate" not in message:
            raise InvalidProviderResponse("Gmail message is missing identity or received time")
        try:
            received = datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz=timezone.utc)
        except (TypeError, ValueError, OverflowError) as exc:
            raise InvalidProviderResponse("Gmail returned invalid internalDate") from exc
        plain = _plain_body(message.get("payload", {}))
        if plain is None:
            plain = _html_body(message.get("payload", {}))
        snippet = message.get("snippet", "")
        if not isinstance(snippet, str):
            raise InvalidProviderResponse("Gmail returned invalid snippet")
        body = plain if plain is not None else snippet
        record = EmailRecord(
            id=email_id, provider="gmail", provider_id=provider_id,
            thread_id=message.get("threadId"), sender=_header(message, "From") or "",
            subject=_header(message, "Subject") or "", received_at=received,
            body_excerpt=(snippet or body)[:500], labels=tuple(message.get("labelIds", [])),
            raw_link_reference=email_id,
            ingestion=IngestionMetadata(self._now(), self._run_id, "gmail-inbox"),
        )
        return ReadEmail(record, body, _header(message, "Message-ID"), _header(message, "References"))

    def _label_ids(self, names: tuple[str, ...]) -> tuple[str, ...]:
        page = self._execute(lambda: self._service.users().labels().list(userId="me"))
        listed = page.get("labels", [])
        if not isinstance(listed, list) or any(not isinstance(item, dict) or
                                               not isinstance(item.get("name"), str) or
                                               not isinstance(item.get("id"), str)
                                               for item in listed):
            raise InvalidProviderResponse("Gmail returned invalid labels")
        labels = {item["name"]: item["id"] for item in listed}
        result = []
        for name in names:
            if name not in labels:
                created = self._execute(
                    lambda: self._service.users().labels().create(
                        userId="me", body={"name": name, "labelListVisibility": "labelShow",
                                           "messageListVisibility": "show"}
                    ), safe_to_retry=False,
                )
                if not isinstance(created.get("id"), str):
                    raise InvalidProviderResponse("Gmail label creation returned no ID")
                labels[name] = created["id"]
            result.append(labels[name])
        return tuple(result)

    def add_labels(self, email_id: str, labels: tuple[str, ...], *, idempotency_key: str) -> None:
        provider_id = _provider_id(email_id)
        if not idempotency_key or not labels:
            raise ValueError("idempotency key and labels are required")
        if any(not name.startswith("KEEPER_") or len(name) > 64 or
               not all(char.isupper() or char.isdigit() or char == "_" for char in name)
               for name in labels):
            raise ValueError("only KEEPER_ labels are permitted")
        previous = self._label_keys.get(idempotency_key)
        if previous is not None and previous != (email_id, labels):
            raise IdempotencyConflict("label retry key reused for another change")
        ids = self._label_ids(labels)
        message = self._execute(
            lambda: self._service.users().messages().get(userId="me", id=provider_id, format="minimal")
        )
        existing = set(message.get("labelIds", []))
        missing = [label_id for label_id in ids if label_id not in existing]
        if missing:
            self._execute(
                lambda: self._service.users().messages().modify(
                    userId="me", id=provider_id, body={"addLabelIds": missing}
                )
            )
        self._label_keys[idempotency_key] = (email_id, labels)

    def _find_draft(self, marker: str, fingerprint: str) -> str | None:
        cursor = None
        scanned = 0
        while True:
            params = {"userId": "me", "maxResults": 100}
            if cursor:
                params["pageToken"] = cursor
            page = self._execute(lambda: self._service.users().drafts().list(**params))
            listed = page.get("drafts", [])
            if not isinstance(listed, list):
                raise InvalidProviderResponse("Gmail returned invalid drafts list")
            for item in listed:
                scanned += 1
                if scanned > 5000:
                    raise ProviderUnavailable("draft scan limit reached; resolve duplicate risk manually")
                draft_id = item.get("id") if isinstance(item, dict) else None
                if not isinstance(draft_id, str) or not draft_id:
                    raise InvalidProviderResponse("Gmail draft has no ID")
                detail = self._execute(
                    lambda: self._service.users().drafts().get(userId="me", id=draft_id, format="raw")
                )
                encoded = detail.get("message", {}).get("raw")
                if not encoded:
                    raise InvalidProviderResponse("Gmail draft has no raw content")
                try:
                    raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
                except (TypeError, ValueError, binascii.Error) as exc:
                    raise InvalidProviderResponse("Gmail draft has invalid raw content") from exc
                parsed = BytesParser(policy=policy.default).parsebytes(raw)
                # MIME folding can leave surrounding whitespace on these long headers.
                if str(parsed.get("X-Keeper-Draft-Key", "")).strip() == marker:
                    if str(parsed.get("X-Keeper-Draft-Fingerprint", "")).strip() != fingerprint:
                        raise IdempotencyConflict("draft retry key reused for different content")
                    return draft_id
            cursor = page.get("nextPageToken")
            if cursor is not None and not isinstance(cursor, str):
                raise InvalidProviderResponse("Gmail returned invalid draft page token")
            if not cursor:
                return None

    def create_draft(self, email_id: str, subject: str, body: str, *, idempotency_key: str) -> str:
        _provider_id(email_id)
        if not idempotency_key or not subject.strip() or not body.strip():
            raise ValueError("draft key, subject, and body are required")
        marker = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        fingerprint = hashlib.sha256(
            json.dumps([email_id, subject, body], ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        existing = self._find_draft(marker, fingerprint)
        if existing:
            return existing
        original = self.read_email(email_id)
        mime = EmailMessage()
        mime["Subject"] = subject
        mime["X-Keeper-Draft-Key"] = marker
        mime["X-Keeper-Draft-Fingerprint"] = fingerprint
        if original.message_id_header:
            mime["In-Reply-To"] = original.message_id_header
            mime["References"] = " ".join(filter(None, [original.references_header, original.message_id_header]))
        mime.set_content(body)
        payload = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")}
        if original.record.thread_id:
            payload["threadId"] = original.record.thread_id
        # No To/Cc/Bcc header, no send endpoint, and no blind retry of create.
        draft = self._execute(
            lambda: self._service.users().drafts().create(userId="me", body={"message": payload}),
            safe_to_retry=False,
        )
        if not isinstance(draft.get("id"), str) or not draft["id"]:
            raise InvalidProviderResponse("Gmail draft creation returned no ID")
        return draft["id"]
