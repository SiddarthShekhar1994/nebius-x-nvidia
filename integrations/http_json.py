"""Small JSON POST transport that never includes credential URLs in errors."""
from __future__ import annotations

import json
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from keeper_core.adapters import InvalidProviderResponse, ProviderUnavailable


class HttpFailure(Exception):
    def __init__(self, status: int, body: dict, retry_after: float | None = None):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body
        self.retry_after = retry_after


def _decode(payload: bytes) -> dict:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise InvalidProviderResponse("provider returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise InvalidProviderResponse("provider response is not an object")
    return value


def post_json(url: str, body: dict, headers: dict[str, str], timeout: float) -> dict:
    request = Request(url, data=json.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read(2_000_001)
    except HTTPError as exc:
        raw = exc.read(2_000_001)
        try:
            error_body = _decode(raw) if len(raw) <= 2_000_000 else {}
        except InvalidProviderResponse:
            error_body = {}
        header = exc.headers.get("Retry-After") if exc.headers else None
        try:
            retry_after = min(max(float(header), 0), 60) if header is not None else None
        except (TypeError, ValueError):
            retry_after = None
        raise HttpFailure(exc.code, error_body, retry_after) from None
    except (URLError, TimeoutError, socket.timeout, OSError):
        raise ProviderUnavailable("provider transport failed or timed out") from None
    if len(payload) > 2_000_000:
        raise InvalidProviderResponse("provider response exceeds size limit")
    return _decode(payload)
