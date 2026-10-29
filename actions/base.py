"""The shape every action step shares. Each step is a thin wrapper over keeper_core.

A step is a module with:
    NAME: str
    wants(email, result) -> bool          which saved decisions it acts on
    run(email, result, ctx) -> Outcome    does the work once; safe to repeat

run() must be idempotent: a second `keeper act` over the same results creates
nothing new. Use stable_key() for provider idempotency keys and database IDs.
In a dry run (ctx.dry_run) it must not touch any provider or keeper.db and
returns status "planned" with what it would do.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from keeper_core.adapters import CalendarAdapter, GmailAdapter, ModelAdapter

# Outcome statuses
DONE = "done"                   # created or changed something
ALREADY_DONE = "already_done"   # a previous run did it; nothing changed
PLANNED = "planned"             # dry run: would have done it
SKIPPED = "skipped"             # nothing to do for this email, with a reason
REVIEW = "review"               # Keeper won't act without a person: unknown company, past date, ...
FAILED = "failed"               # provider or data error; detail says which class, never secrets
STATUSES = (DONE, ALREADY_DONE, PLANNED, SKIPPED, REVIEW, FAILED)


@dataclass(frozen=True)
class ActionContext:
    db_path: Path
    gmail: GmailAdapter
    calendar: CalendarAdapter
    model: ModelAdapter | None      # drafts use Super through the router
    now: Callable[[], datetime]     # UTC; injected so tests are deterministic
    dry_run: bool = True


@dataclass(frozen=True)
class Outcome:
    step: str
    email_id: str
    status: str
    detail: str                     # one line for the summary; no email bodies or secrets
    provider_id: str | None = None  # draft or calendar event ID when one exists

    def __post_init__(self):
        if self.status not in STATUSES:
            raise ValueError(f"unknown outcome status {self.status!r}")


def connect(db_path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """A connection with foreign keys on; read-only when nothing may change (dry runs)."""
    if read_only:
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def linked_application(conn: sqlite3.Connection, email_id: str) -> tuple[str, str] | None:
    """(application ID, opportunity ID) for an application this email is recorded as evidence for."""
    return conn.execute(
        """SELECT a.id, a.opportunity_id FROM application_source_emails s
           JOIN applications a ON a.id = s.application_id WHERE s.email_id = ? ORDER BY s.rowid LIMIT 1""",
        (email_id,),
    ).fetchone()
