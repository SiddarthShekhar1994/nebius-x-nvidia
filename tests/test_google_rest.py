"""Offline tests for the urllib Gmail and Calendar transport and its sandbox policy."""
import io
import json
import re
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from integrations import calendar as calendar_module
from integrations.calendar import LiveCalendar
from integrations.gmail import LiveGmail
from integrations.google_rest import (CALENDAR_URL, GMAIL_URL, ROUTES, TOKEN_URL, AccessToken,
                                      GoogleHttpError, GoogleService)
from keeper_core.adapters import (AuthenticationError, IdempotencyConflict, ProviderNotFound,
                                  RateLimited)
from keeper_core.db import migrate
from keeper_core.time import to_storage

POLICY = Path(__file__).resolve().parents[1] / "agent" / "openshell" / "policies" / "keeper-google.yaml"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify",
          "https://www.googleapis.com/auth/calendar.events"]


class Answer:
    def __init__(self, payload: bytes):
        self.payload = payload

    def read(self, limit=-1):
        return self.payload if limit is None or limit < 0 else self.payload[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGoogle:
    """Stands in for urlopen: Google's token endpoint plus scripted REST answers."""

    def __init__(self):
        self.requests = []
        self.refreshes = 0
        self.token_status = 200
        self.scripted = {}

    def answer(self, method, path, *replies):
        """Queue (status, body, headers) replies; the last one repeats."""
        self.scripted[(method, path)] = [reply if isinstance(reply, tuple) else (200, reply, {})
                                         for reply in replies]

    def __call__(self, request, timeout):
        self.requests.append(request)
        if request.full_url == TOKEN_URL:
            if self.token_status != 200:
                raise _error(request, self.token_status, {"error": "invalid_grant"}, {})
            self.refreshes += 1
            return Answer(json.dumps({"access_token": f"access-{self.refreshes}",
                                      "expires_in": 3600}).encode())
        key = (request.get_method(), urlsplit(request.full_url).path)
        queue = self.scripted[key]
        status, body, headers = queue.pop(0) if len(queue) > 1 else queue[0]
        if callable(body):
            body = body(request)
        if status >= 400:
            raise _error(request, status, body, headers)
        return Answer(json.dumps(body).encode())


def _error(request, status, body, headers):
    message = Message()
    for name, value in headers.items():
        message[name] = value
    return HTTPError(request.full_url, status, "error", message, io.BytesIO(json.dumps(body).encode()))


def write_token(folder, scopes=SCOPES):
    path = Path(folder) / "google-token.json"
    path.write_text(json.dumps({"token": "stale", "refresh_token": "refresh-me",
                                "client_id": "client", "client_secret": "secret",
                                "token_uri": TOKEN_URL, "scopes": scopes}), encoding="utf-8")
    return path


class AccessTokenTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.google = FakeGoogle()
        self.now = [0.0]
        self.token = AccessToken(write_token(self.temp.name), opener=self.google, clock=lambda: self.now[0])

    def tearDown(self):
        self.temp.cleanup()

    def test_refreshes_once_then_reuses_until_near_expiry(self):
        self.assertEqual(self.token.get(), "access-1")
        self.assertEqual(self.token.get(), "access-1")
        self.now[0] = 3600 - 30  # inside the one-minute margin
        self.assertEqual(self.token.get(), "access-2")
        grant = parse_qs(self.google.requests[0].data.decode())
        self.assertEqual(grant, {"grant_type": ["refresh_token"], "refresh_token": ["refresh-me"],
                                 "client_id": ["client"], "client_secret": ["secret"]})

    def test_refresh_goes_to_google_whatever_the_file_says(self):
        path = Path(self.temp.name) / "google-token.json"
        data = json.loads(path.read_text())
        path.write_text(json.dumps({**data, "token_uri": "https://attacker.example/token"}))
        AccessToken(path, opener=self.google).get()
        self.assertEqual(self.google.requests[-1].full_url, TOKEN_URL)

    def test_revoked_refresh_token_reads_as_rejected_login(self):
        self.google.token_status = 400
        with self.assertRaises(GoogleHttpError) as caught:
            self.token.get()
        self.assertEqual(caught.exception.resp.status, 401)

    def test_token_file_needs_refresh_credentials(self):
        path = Path(self.temp.name) / "partial.json"
        path.write_text(json.dumps({"client_id": "client", "scopes": SCOPES}))
        with self.assertRaises(ValueError):
            AccessToken(path)


class GoogleServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.google = FakeGoogle()
        token = AccessToken(write_token(self.temp.name), opener=self.google)
        self.gmail = GoogleService(GMAIL_URL, token, opener=self.google)
        self.calendar = GoogleService(CALENDAR_URL, token, opener=self.google)

    def tearDown(self):
        self.temp.cleanup()

    def test_query_is_encoded_like_googleapiclient(self):
        self.google.answer("GET", "/gmail/v1/users/me/messages", {"messages": []})
        self.gmail.users().messages().list(userId="me", labelIds=["INBOX", "UNREAD"], maxResults=50,
                                           pageToken=None, includeSpamTrash=False).execute()
        request = self.google.requests[-1]
        self.assertEqual(request.full_url, GMAIL_URL + "/users/me/messages?labelIds=INBOX&labelIds=UNREAD"
                                                       "&maxResults=50&includeSpamTrash=false")
        self.assertEqual(request.get_header("Authorization"), "Bearer access-1")
        self.assertIsNone(request.data)

    def test_body_is_json_and_path_parameters_are_quoted(self):
        self.google.answer("POST", "/calendar/v3/calendars/demo%40example.test/events", {"id": "keeperabc"})
        created = self.calendar.events().insert(calendarId="demo@example.test", body={"summary": "Apply"},
                                                sendUpdates="none").execute()
        request = self.google.requests[-1]
        self.assertEqual(created, {"id": "keeperabc"})
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith("/calendars/demo%40example.test/events?sendUpdates=none"))
        self.assertEqual(json.loads(request.data), {"summary": "Apply"})
        self.assertEqual(request.get_header("Content-type"), "application/json")

    def test_nothing_is_sent_until_execute(self):
        call = self.gmail.users().getProfile(userId="me")
        self.assertEqual(self.google.requests, [])
        self.google.answer("GET", "/gmail/v1/users/me/profile", {"emailAddress": "demo@example.test"})
        self.assertEqual(call.execute()["emailAddress"], "demo@example.test")

    def test_a_lapsed_access_token_is_refreshed_once(self):
        self.google.answer("GET", "/gmail/v1/users/me/profile", (401, {}, {}), {"emailAddress": "a@b.test"})
        self.assertEqual(self.gmail.users().getProfile(userId="me").execute(), {"emailAddress": "a@b.test"})
        self.assertEqual(self.google.refreshes, 2)
        self.assertEqual(self.google.requests[-1].get_header("Authorization"), "Bearer access-2")

    def test_errors_carry_what_the_adapters_read(self):
        body = {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
        self.google.answer("GET", "/gmail/v1/users/me/labels", (429, body, {"Retry-After": "7"}))
        with self.assertRaises(GoogleHttpError) as caught:
            self.gmail.users().labels().list(userId="me").execute()
        self.assertNotIsInstance(caught.exception, OSError)  # adapters treat OSError as transport failure
        self.assertEqual(caught.exception.resp.status, 429)
        self.assertEqual(caught.exception.resp.get("retry-after"), "7")
        self.assertEqual(json.loads(caught.exception.content), body)

    def test_only_keepers_calls_exist(self):
        with self.assertRaises(AttributeError):
            self.gmail.users().messages().send
        with self.assertRaises(TypeError):
            self.gmail.users().messages().get(userId="me")


class AdapterOverRestTest(unittest.TestCase):
    """LiveGmail and LiveCalendar keep their retry and error mapping on this transport."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.google = FakeGoogle()
        self.token = AccessToken(write_token(self.temp.name), opener=self.google)
        self.waits = []

    def tearDown(self):
        self.temp.cleanup()

    def live_gmail(self):
        return LiveGmail(GoogleService(GMAIL_URL, self.token, opener=self.google), run_id="test",
                         sleep=self.waits.append)

    def test_gmail_labels_and_error_mapping(self):
        labels = {"KEEPER_DUE": None}

        def created(request):
            labels["KEEPER_DUE"] = "Label_1"
            return {"id": "Label_1", "name": json.loads(request.data)["name"]}

        self.google.answer("GET", "/gmail/v1/users/me/labels", {"labels": []})
        self.google.answer("POST", "/gmail/v1/users/me/labels", (200, created, {}))
        self.google.answer("GET", "/gmail/v1/users/me/messages/abc123", {"id": "abc123", "labelIds": ["INBOX"]})
        self.google.answer("POST", "/gmail/v1/users/me/messages/abc123/modify", {"id": "abc123"})
        self.live_gmail().add_labels("gmail:abc123", ("KEEPER_DUE",), idempotency_key="k1")
        modify = self.google.requests[-1]
        self.assertEqual(json.loads(modify.data), {"addLabelIds": ["Label_1"]})
        self.assertIn("format=minimal", self.google.requests[-2].full_url)

        rate = {"error": {"errors": [{"reason": "rateLimitExceeded"}]}}
        self.google.answer("GET", "/gmail/v1/users/me/profile", (429, rate, {"Retry-After": "2"}),
                           {"emailAddress": "Demo@Example.test"})
        self.live_gmail().verify_account("demo@example.test")
        self.assertEqual(self.waits, [2.0])

        self.google.answer("GET", "/gmail/v1/users/me/profile", (429, rate, {"Retry-After": "1"}))
        with self.assertRaises(RateLimited):
            self.live_gmail().check_health()
        self.google.answer("GET", "/gmail/v1/users/me/profile", (403, {"error": {"errors": []}}, {}))
        with self.assertRaises(AuthenticationError):
            self.live_gmail().check_health()
        self.google.answer("GET", "/gmail/v1/users/me/messages/gone", (404, {}, {}))
        with self.assertRaises(ProviderNotFound):
            self.live_gmail().add_labels("gmail:gone", ("KEEPER_DUE",), idempotency_key="k2")

    def test_calendar_insert_conflict_still_recovers(self):
        db = Path(self.temp.name) / "keeper.db"
        migrate(db)
        deadline = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
        stamp = to_storage(deadline)
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("""INSERT INTO opportunities
                (id, dedupe_key, canonical_url, organization, title, source, discovered_at, eligibility)
                VALUES ('opp:1', 'key:1', 'https://example.test/1', 'Example', 'Intern', 'test', ?, 'eligible')""",
                         (stamp,))
            conn.execute("""INSERT INTO deadline_events
                (id, opportunity_id, deadline_at, evidence, created_at, updated_at)
                VALUES ('deadline:1', 'opp:1', ?, 'test', ?, ?)""", (stamp, stamp, stamp))
        events_path = "/calendar/v3/calendars/primary/events"
        event_id = calendar_module._provider_id("deadline:1")
        # An earlier run created the event, but the key search hasn't indexed it yet.
        earlier = {"id": event_id, "summary": "Old", "start": {"dateTime": "2026-11-01T12:00:00Z"},
                   "extendedProperties": {"private": {"keeperDeadlineKey": calendar_module._event_key("deadline:1")}}}
        self.google.answer("GET", events_path, {"items": []})
        self.google.answer("POST", events_path, (409, {"error": {"errors": [{"reason": "duplicate"}]}}, {}))
        self.google.answer("GET", f"{events_path}/{event_id}", earlier)
        self.google.answer("PATCH", f"{events_path}/{event_id}", (200, lambda request: json.loads(request.data), {}))
        calendar = LiveCalendar(GoogleService(CALENDAR_URL, self.token, opener=self.google), db,
                                sleep=self.waits.append, now=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(calendar.create_deadline("deadline:1", deadline, "Apply", idempotency_key="key:1"),
                         event_id)
        sent = [request for request in self.google.requests if request.full_url.startswith(CALENDAR_URL)]
        self.assertEqual([request.get_method() for request in sent], ["GET", "POST", "GET", "PATCH"])
        self.assertIn("sendUpdates=none", sent[1].full_url)
        self.assertEqual(json.loads(sent[3].data)["start"], {"dateTime": "2026-11-05T12:00:00Z"})

        # A 409 for an event that belongs to something else is never taken over.
        earlier["extendedProperties"]["private"]["keeperDeadlineKey"] = "another-deadline"
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("UPDATE deadline_events SET provider = NULL, provider_event_id = NULL")
        fresh = LiveCalendar(GoogleService(CALENDAR_URL, self.token, opener=self.google), db,
                             now=lambda: datetime(2026, 11, 1, tzinfo=timezone.utc))
        with self.assertRaises(IdempotencyConflict):
            fresh.create_deadline("deadline:1", deadline, "Apply", idempotency_key="key:1")


class TokenFileTest(unittest.TestCase):
    def test_adapters_build_from_the_runtime_token_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            path = write_token(folder)
            self.assertIsInstance(LiveGmail.from_token_file(path, run_id="t"), LiveGmail)
            self.assertIsInstance(LiveCalendar.from_token_file(path, Path(folder) / "keeper.db"), LiveCalendar)
            seed_only = write_token(folder, scopes=["https://www.googleapis.com/auth/gmail.insert"])
            with self.assertRaises(AuthenticationError):
                LiveGmail.from_token_file(seed_only, run_id="t")
            with self.assertRaises(AuthenticationError):
                LiveCalendar.from_token_file(seed_only, Path(folder) / "keeper.db")


def policy_rules():
    """host -> (allow, deny) lists of (method, path glob), read without a YAML parser."""
    rules, host = {}, None
    for line in POLICY.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("- host:"):
            host = line.split(":", 1)[1].strip()
            rules[host] = ([], [])
            continue
        match = re.search(r'method:\s*"?([A-Z*]+)"?,\s*path:\s*"([^"]+)"', line)
        if match and host:
            rules[host][0 if "allow:" in line else 1].append(match.groups())
    return rules


def matches(rule, method, path):
    # OpenShell globs: "*" stays within one path segment.
    rule_method, pattern = rule
    regex = "".join("[^/]*" if part == "*" else re.escape(part) for part in re.split(r"(\*)", pattern))
    return rule_method in ("*", method) and re.fullmatch(regex, path) is not None


def keeper_requests():
    samples = {"userId": "me", "id": "18c2f0a1b2", "calendarId": "primary", "eventId": "keeperabc123"}
    for base, routes in ROUTES.items():
        base = urlsplit(base)
        for method, template in routes.values():
            yield base.hostname, method, base.path + template.format(**samples)
    yield urlsplit(TOKEN_URL).hostname, "POST", urlsplit(TOKEN_URL).path


class PolicyTest(unittest.TestCase):
    def test_policy_opens_every_call_keeper_makes(self):
        rules = policy_rules()
        for host, method, path in keeper_requests():
            with self.subTest(host=host, method=method, path=path):
                allow, deny = rules[host]
                self.assertTrue(any(matches(rule, method, path) for rule in allow))
                self.assertFalse(any(matches(rule, method, path) for rule in deny))

    def test_policy_opens_nothing_keeper_does_not_call(self):
        calls = list(keeper_requests())
        for host, (allow, _) in policy_rules().items():
            for rule in allow:
                with self.subTest(host=host, rule=rule):
                    self.assertTrue(any(h == host and matches(rule, m, p) for h, m, p in calls))

    def test_sending_mail_is_denied(self):
        allow, deny = policy_rules()["gmail.googleapis.com"]
        for path in ("/gmail/v1/users/me/messages/send", "/gmail/v1/users/me/drafts/send",
                     "/upload/gmail/v1/users/me/messages/send", "/upload/gmail/v1/users/me/drafts/send"):
            with self.subTest(path=path):
                self.assertTrue(any(matches(rule, "POST", path) for rule in deny))
                self.assertFalse(any(matches(rule, "POST", path) for rule in allow))


if __name__ == "__main__":
    unittest.main()
