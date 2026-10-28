"""The shape every action step shares, so the three owners can build in parallel.

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

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from keeper_core.adapters import CalendarAdapter, GmailAdapter, ModelAdapter

# Outcome statuses
DONE = "done"                   # created or changed something
ALREADY_DONE = "already_done"   # a previous run did it; nothing changed
PLANNED = "planned"             # dry run: would have done it
SKIPPED = "skipped"             # wanted the email but decided not to act, with a reason
FAILED = "failed"               # provider or data error; detail says which class, never secrets
NOT_BUILT = "not_built"         # placeholder until the owner's module lands
STATUSES = (DONE, ALREADY_DONE, PLANNED, SKIPPED, FAILED, NOT_BUILT)


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
