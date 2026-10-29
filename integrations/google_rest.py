"""Gmail and Calendar over Google's REST API, using only the standard library.

Inside the OpenShell sandbox Google's client library can't reach Google: it
sends through httplib2, which needs PySocks to use HTTPS_PROXY and trusts
certifi rather than the sandbox's CA bundle, and the Hermes image ships the
library only for Google Chat. urllib honours HTTPS_PROXY and SSL_CERT_FILE,
as the Telegram and Tavily adapters already rely on.

GoogleService implements just the googleapiclient calls LiveGmail and
LiveCalendar make (ROUTES below), and its errors carry .resp.status,
.resp["retry-after"] and .content like googleapiclient's HttpError, so the
adapters' retry and error handling is unchanged. The sandbox network policy
(agent/openshell/policies/keeper-google.yaml) opens exactly these routes.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from keeper_core.adapters import InvalidProviderResponse

GMAIL_URL = "https://gmail.googleapis.com/gmail/v1"
CALENDAR_URL = "https://www.googleapis.com/calendar/v3"
# Fixed rather than read from the token file, so an edited token can't send the
# refresh secret anywhere else. It's the only sign-in host the policy opens.
TOKEN_URL = "https://oauth2.googleapis.com/token"
TIMEOUT = 30.0
MAX_RESPONSE = 30_000_000  # a full Gmail message can approach 25 MB
MAX_ERROR = 100_000

ROUTES = {
    GMAIL_URL: {
        ("users", "getProfile"): ("GET", "/users/{userId}/profile"),
        ("messages", "list"): ("GET", "/users/{userId}/messages"),
        ("messages", "get"): ("GET", "/users/{userId}/messages/{id}"),
        ("messages", "modify"): ("POST", "/users/{userId}/messages/{id}/modify"),
        ("labels", "list"): ("GET", "/users/{userId}/labels"),
        ("labels", "create"): ("POST", "/users/{userId}/labels"),
        ("drafts", "list"): ("GET", "/users/{userId}/drafts"),
        ("drafts", "get"): ("GET", "/users/{userId}/drafts/{id}"),
        ("drafts", "create"): ("POST", "/users/{userId}/drafts"),
    },
    CALENDAR_URL: {
        ("events", "list"): ("GET", "/calendars/{calendarId}/events"),
        ("events", "get"): ("GET", "/calendars/{calendarId}/events/{eventId}"),
        ("events", "insert"): ("POST", "/calendars/{calendarId}/events"),
        ("events", "patch"): ("PATCH", "/calendars/{calendarId}/events/{eventId}"),
    },
}

Opener = Callable[..., object]


class GoogleHttpError(Exception):
    """A non-2xx answer, shaped like googleapiclient.errors.HttpError."""

    def __init__(self, status: int, content: bytes = b"", headers: dict | None = None):
        super().__init__(f"Google request failed (HTTP {status})")
        self.resp = _Response(status, headers or {})
        self.content = content


class _Response(dict):
    def __init__(self, status: int, headers: dict):
        super().__init__((str(key).lower(), value) for key, value in headers.items())
        self.status = status


def _http_error(exc: HTTPError, status: int | None = None) -> GoogleHttpError:
    try:
        content = exc.read(MAX_ERROR)
    except OSError:
        content = b""
    finally:
        exc.close()
    headers = dict(exc.headers.items()) if exc.headers else {}
    return GoogleHttpError(status or exc.code, content, headers)


class AccessToken:
    """Short-lived access tokens from the runtime token file's refresh token."""

    def __init__(self, token_file: Path, *, opener: Opener = urlopen,
                 clock: Callable[[], float] = time.monotonic):
        data = json.loads(Path(token_file).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Google token must be a JSON object")
        self.scopes = tuple(scope for scope in data.get("scopes") or () if isinstance(scope, str))
        self._grant = {"grant_type": "refresh_token", "refresh_token": data.get("refresh_token"),
                       "client_id": data.get("client_id"), "client_secret": data.get("client_secret")}
        if not all(isinstance(value, str) and value for value in self._grant.values()):
            raise ValueError("Google token lacks refresh_token, client_id or client_secret")
        self._opener = opener
        self._clock = clock
        self._value: str | None = None
        self._expires = 0.0

    def get(self, *, refresh: bool = False) -> str:
        if refresh or self._value is None or self._clock() >= self._expires:
            self._value, lifetime = self._refresh()
            self._expires = self._clock() + max(lifetime - 60.0, 0.0)
        return self._value

    def _refresh(self) -> tuple[str, float]:
        request = Request(TOKEN_URL, data=urlencode(self._grant).encode("ascii"), method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self._opener(request, timeout=TIMEOUT) as response:
                data = json.loads(response.read(MAX_ERROR))
        except HTTPError as exc:
            # 400 is invalid_grant: the refresh token was revoked or expired
            # (seven days for an app in Testing). Report it as a rejected login.
            raise _http_error(exc, 401 if exc.code == 400 else None) from None
        except ValueError:
            raise InvalidProviderResponse("Google token response is not JSON") from None
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise InvalidProviderResponse("Google token response has no access token")
        try:
            lifetime = float(data.get("expires_in", 3600))
        except (TypeError, ValueError):
            lifetime = 3600.0
        return token, lifetime


class GoogleService:
    """The slice of googleapiclient's Gmail or Calendar service Keeper calls."""

    def __init__(self, base_url: str, token: AccessToken, *, opener: Opener = urlopen):
        self.routes = ROUTES[base_url]
        self._base_url = base_url
        self._token = token
        self._opener = opener

    def users(self) -> "_Resource":
        return _Resource(self, "users")

    def events(self) -> "_Resource":
        return _Resource(self, "events")

    def send(self, method: str, path: str, query: dict, body: dict | None) -> object:
        encoded = _query(query)
        url = self._base_url + path + (f"?{encoded}" if encoded else "")
        data = None if body is None else json.dumps(body).encode("utf-8")
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self._token.get(refresh=attempt == 2)}",
                       "Accept": "application/json"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            try:
                with self._opener(Request(url, data=data, headers=headers, method=method),
                                  timeout=TIMEOUT) as response:
                    payload = response.read(MAX_RESPONSE + 1)
                break
            except HTTPError as exc:
                if exc.code == 401 and attempt == 1:
                    exc.close()
                    continue  # the access token lapsed early; refresh it once
                raise _http_error(exc) from None
        if len(payload) > MAX_RESPONSE:
            raise InvalidProviderResponse("Google response exceeds size limit")
        if not payload.strip():
            return {}
        try:
            return json.loads(payload)
        except ValueError:
            raise InvalidProviderResponse("Google returned invalid JSON") from None


class _Resource:
    def __init__(self, service: GoogleService, name: str):
        self._service = service
        self._name = name

    # Gmail's users() resource holds these three.
    def messages(self) -> "_Resource":
        return _Resource(self._service, "messages")

    def labels(self) -> "_Resource":
        return _Resource(self._service, "labels")

    def drafts(self) -> "_Resource":
        return _Resource(self._service, "drafts")

    def __getattr__(self, method: str):
        if method.startswith("_"):
            raise AttributeError(method)
        route = self._service.routes.get((self._name, method))
        if route is None:
            raise AttributeError(f"{self._name}().{method}() is not supported")
        return lambda **params: _Call(self._service, route, params)


class _Call:
    """googleapiclient's HttpRequest: nothing is sent until execute()."""

    def __init__(self, service: GoogleService, route: tuple[str, str], params: dict):
        method, template = route
        params = dict(params)
        body = params.pop("body", None)
        path = template
        while "{" in path:
            name = path[path.index("{") + 1:path.index("}")]
            value = params.pop(name, None)
            if not isinstance(value, str) or not value:
                raise TypeError(f"missing required parameter {name}")
            path = path.replace("{" + name + "}", quote(value, safe=""), 1)
        self._service = service
        self._request = (method, path, params, body)

    def execute(self) -> object:
        return self._service.send(*self._request)


def _query(params: dict) -> str:
    """Encode like googleapiclient: lists repeat the key, booleans are lowercase."""
    pairs = []
    for key, value in params.items():
        for item in value if isinstance(value, (list, tuple)) else (value,):
            if item is None:
                continue
            pairs.append((key, ("true" if item else "false") if isinstance(item, bool) else str(item)))
    return urlencode(pairs)
