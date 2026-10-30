"""The hosted demo's engine: the internship story and visitors' own emails, on stand-in Gmail and Calendar.

Keeper's own code makes every decision here. Triage runs on Nemotron 3 Nano, then the tracker,
calendar deadlines and reply drafts (written by Nemotron 3 Super) run as `keeper act` would,
all through the router and Token Factory. Only Gmail and Google Calendar are in-memory
stand-ins (keeper_core.fakes), so a visitor needs no Google account and nothing is ever sent.
The demo clock stays in the story's week, so its deadlines are still ahead whenever it runs.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path

from actions.__main__ import act
from actions.base import ActionContext
from keeper_core import application_cli
from keeper_core.adapters import AuthenticationError
from keeper_core.applications import list_applications
from keeper_core.contracts import EmailRecord, IngestionMetadata
from keeper_core.db import migrate
from keeper_core.fakes import FakeCalendar, FakeGmail
from keeper_core.time import to_storage
from router import calllog
from triage.run import run_triage
from triage.store import ResultStore, saved_decisions

FIXTURES = Path(__file__).resolve().parents[1] / "demo" / "fixtures"
CLOCK = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)  # the Friday after the story's emails
ACCOUNT = "student@example.test"
TRACK = ["add", "--organization", "Example Inc", "--title", "Software Engineer Intern",
         "--url", "https://careers.example.test/jobs/software-engineer-intern",
         "--found-at", "2026-10-26T12:00:00Z"]  # as agent/demo-story.sh tracks it
# The most model calls each step can make: one triage per email, one draft per actionable email.
CALLS = {"track": 0, "part1": 5, "part2": 2, "email": 2}
NEEDS = {"part1": "track", "part2": "part1"}
LIMITS = {"sender": 200, "subject": 200, "body": 1500}
MAX_WRITTEN = 30
_STDOUT = threading.Lock()  # redirect_stdout swaps a process-wide stream


class StepError(ValueError):
    """A step this visitor can't run now; the message is written to be shown to them."""


@dataclass
class Session:
    """One visitor's Keeper: its own database, router log, inbox and calendar."""
    directory: Path
    gmail: FakeGmail = field(default_factory=lambda: FakeGmail(()))
    calendar: FakeCalendar = field(default_factory=FakeCalendar)
    lock: threading.Lock = field(default_factory=threading.Lock)
    done: set = field(default_factory=set)
    cards: list = field(default_factory=list)
    written: int = 0
    calls: int = 0
    job: str | None = None

    def __post_init__(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        migrate(self.db)

    @property
    def db(self) -> Path:
        return self.directory / "keeper.db"

    @property
    def router_db(self) -> Path:
        return self.directory / "router.db"


class Budget:
    """Model calls left today across every visitor, so a public page can't run up the bill."""

    def __init__(self, per_day: int, clock=lambda: datetime.now(timezone.utc)):
        self.per_day = per_day
        self._clock = clock
        self._lock = threading.Lock()
        self._day = None
        self._used = 0

    def reserve(self, calls: int) -> bool:
        with self._lock:
            today = self._clock().date()
            if today != self._day:
                self._day, self._used = today, 0
            if self._used + calls > self.per_day:
                return False
            self._used += calls
            return True

    def used(self) -> int:
        with self._lock:
            return self._used if self._day == self._clock().date() else 0


def live_models(session: Session):
    """Nano for triage and Super for drafts, logging this visitor's calls to its own router log."""
    from integrations.draft_model import DraftRouterModel
    from router.config import load_settings
    from triage.model import RouterModel
    settings = load_settings({**os.environ, "KEEPER_ROUTER_DB": str(session.router_db)})
    return RouterModel(settings=settings), DraftRouterModel(settings)


def story(part: int) -> list[dict]:
    return json.loads((FIXTURES / f"story_part{part}.json").read_text(encoding="utf-8"))["emails"]


def _email(provider_id, thread_id, sender, subject, at, body) -> EmailRecord:
    return EmailRecord(f"gmail:{provider_id}", "gmail", provider_id, thread_id, sender, subject, at, body,
                       ("INBOX",), None, IngestionMetadata(at, "web-demo", "gmail-inbox"))


def story_email(item: dict) -> EmailRecord:
    at = datetime.fromisoformat(item["received_at"].replace("Z", "+00:00"))
    return _email(item["id"], item["thread_id"], item["sender"], item["subject"], at, item["body_text"])


def written_email(session: Session, sender, subject, body) -> EmailRecord:
    """A visitor's email, received just before the demo clock and after any they wrote earlier."""
    for name, value in (("sender", sender), ("subject", subject), ("body", body)):
        if not isinstance(value, str) or not value.strip() or len(value) > LIMITS[name]:
            raise StepError(f"The {name} needs 1 to {LIMITS[name]} characters.")
    if "@" not in parseaddr(sender)[1]:
        raise StepError("The sender needs an email address, like Name <name@example.com>.")
    if session.written >= MAX_WRITTEN:
        raise StepError("This visit has sent Keeper its limit of emails. Start over to write more.")
    session.written += 1
    at = CLOCK - timedelta(minutes=60 - session.written)
    provider_id = f"visitor-{session.written:02d}"
    return _email(provider_id, f"thread-{provider_id}", sender.strip(), subject.strip(), at, body.strip())


def check(session: Session, action: str) -> None:
    if action not in CALLS:
        raise StepError("Unknown step.")
    if action in NEEDS or action == "track":
        if action in session.done:
            raise StepError("That step already ran. Start over to replay the story.")
        if NEEDS.get(action) and NEEDS[action] not in session.done:
            raise StepError("Run the story's steps in order.")


def shown(email: EmailRecord) -> dict:
    return {"id": email.provider_id, "sender": email.sender, "subject": email.subject.removeprefix("[SYNTHETIC] "),
            "body": email.body_excerpt, "received_at": to_storage(email.received_at)}


@dataclass
class _Inbox:
    """triage's source protocol for one stand-in Gmail message, so its labels land on FakeGmail."""
    gmail: FakeGmail
    email: EmailRecord
    account: str = ACCOUNT
    can_apply: bool = True

    def identities(self, limit):
        return (self.email.id,)

    def read(self, identity):
        return self.email, self.email.body_excerpt, self.email.provider_id

    def fixture_id(self, identity):
        return self.email.provider_id

    def apply_labels(self, identity, labels, *, idempotency_key):
        self.gmail.add_labels(identity, labels, idempotency_key=idempotency_key)


def receive(session: Session, email: EmailRecord, triage_model, draft_model) -> dict:
    """Sort one email with Nemotron and label it, then let Keeper act on it: one card for the page."""
    if not any(known.id == email.id for known in session.gmail.emails):
        session.gmail.emails += (email,)
    report = run_triage(_Inbox(session.gmail, email), triage_model, ResultStore(session.db, account=ACCOUNT),
                        save=True, apply=True, classified_at=CLOCK)
    card = {"email": shown(email), "triage": None, "labels": [], "outcomes": [], "drafts": [], "error": None}
    if report["fatal_error"] == "AuthenticationError":
        raise AuthenticationError("Token Factory rejected the demo's key")
    if report["errors"] or not report["results"]:
        card["error"] = report["errors"][0]["error_type"] if report["errors"] else "no result"
        return card
    row = report["results"][0]
    card["triage"] = {name: row[name] for name in
                      ("bucket", "reason", "career", "scam_signals", "extracted_deadline")}
    model = row.get("model") or {}
    card["triage"]["model"] = model.get("served_model") or model.get("requested_model")
    card["labels"] = sorted(session.gmail.labels_by_email.get(email.id, ()))
    pairs = [pair for pair in saved_decisions(session.db) if pair[0].id == email.id]
    ctx = ActionContext(session.db, session.gmail, session.calendar, draft_model, lambda: CLOCK, dry_run=False)
    card["outcomes"] = [{"step": o.step, "status": o.status, "detail": o.detail} for o in act(pairs, ctx)]
    card["drafts"] = _drafts(session, email.id)
    return card


def _drafts(session: Session, email_id: str | None = None) -> list[dict]:
    """Saved drafts; review flags mean Keeper wrote a holding reply instead of asking Super."""
    query = ("SELECT email_id, subject, body, review_flags_json FROM draft_records "
             "WHERE provider_draft_id IS NOT NULL" + (" AND email_id = ?" if email_id else "") +
             " ORDER BY created_at, rowid")
    with closing(sqlite3.connect(session.db)) as conn:
        rows = conn.execute(query, (email_id,) if email_id else ()).fetchall()
    return [{"email_id": row[0].removeprefix("gmail:"), "subject": row[1], "body": row[2],
             "flags": json.loads(row[3] or "[]")} for row in rows]


def track(session: Session) -> dict:
    output = io.StringIO()
    with _STDOUT, contextlib.redirect_stdout(output):
        code = application_cli.main(["--db", str(session.db)] + TRACK)
    if code != 0:
        raise StepError("Keeper couldn't start tracking the internship.")
    return json.loads(output.getvalue())


def run_step(session: Session, action: str, models, emit, *, email: EmailRecord | None = None) -> None:
    """Run one checked step for this visitor, emitting page events as Keeper works."""
    if action == "track":
        emit({"kind": "tracked", "application": track(session)})
        session.done.add(action)
        return
    triage_model, draft_model = models(session)
    emails = [story_email(item) for item in story(int(action[-1]))] if action in NEEDS else [email]
    failed = False
    for message in emails:
        emit({"kind": "arrived", "email": shown(message)})
        card = receive(session, message, triage_model, draft_model)
        session.cards.append(card)
        emit({"kind": "card", "card": card})
        failed = failed or card["error"] is not None
    if action in NEEDS and not failed:
        session.done.add(action)


def state(session: Session) -> dict:
    tracker = []
    with closing(sqlite3.connect(session.db)) as conn:
        for app in list_applications(conn):
            organization, title = conn.execute(
                "SELECT organization, title FROM opportunities WHERE id = ?", (app.opportunity_id,)).fetchone()
            tracker.append({
                "organization": organization, "title": title, "status": app.status.value,
                "outcome": app.decision_outcome,
                "history": [{"status": change.status.value, "at": to_storage(change.changed_at)}
                            for change in app.status_history],
            })
    usage = [{"model": model, "calls": calls, "errors": errors or 0, "tokens": prompt + completion,
              "latency_ms": latency}
             for model, calls, errors, prompt, completion, latency, _ in calllog.summary(session.router_db)
             ] if session.router_db.exists() else []
    return {
        "tracker": tracker,
        "calendar": [{"summary": summary, "at": to_storage(at)}
                     for _, _, at, summary in session.calendar.events_by_key.values()],
        "drafts": _drafts(session),
        "usage": usage, "done": sorted(session.done), "cards": session.cards, "calls": session.calls,
    }
