"""Stable top-five Telegram digest for newly discovered opportunities."""
from __future__ import annotations

import sqlite3
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .adapters import TelegramAdapter
from .time import to_storage
from .workflow import stable_key


@dataclass(frozen=True, slots=True)
class DigestResult:
    status: str  # sent, empty
    message_id: str | None
    opportunity_ids: tuple[str, ...]
    text: str | None


def _new_candidates(conn: sqlite3.Connection, run_id: str) -> tuple[tuple, ...]:
    return tuple(conn.execute(
        """SELECT o.opportunity_id, p.organization, p.title, o.observed_url,
                  o.score, o.explanation, o.eligibility
           FROM opportunity_observations AS o
           JOIN opportunities AS p ON p.id = o.opportunity_id
           WHERE o.run_id = ? AND o.score IS NOT NULL
             AND o.eligibility != 'ineligible'
             AND NOT EXISTS (
                 SELECT 1 FROM opportunity_observations AS prior
                 WHERE prior.opportunity_id = o.opportunity_id
                   AND (prior.observed_at < o.observed_at OR
                        (prior.observed_at = o.observed_at AND prior.run_id < o.run_id))
             )
           ORDER BY o.score DESC, CASE o.eligibility WHEN 'eligible' THEN 0 ELSE 1 END,
                    p.organization, p.title LIMIT 5""",
        (run_id,),
    ).fetchall())


def _one_line(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _digest_text(rows: tuple[tuple, ...]) -> str:
    lines = ["Keeper opportunity radar — new matches"]
    for index, (_, organization, title, url, score, reason, eligibility) in enumerate(rows, 1):
        lines.append(
            f"{index}. {_one_line(organization, 70)} — {_one_line(title, 100)} "
            f"| {score}/10 | {eligibility}\n"
            f"   {_one_line(reason or 'Relevant role', 220)}\n   {url}"
        )
    return "\n".join(lines)


def send_radar_digest(conn: sqlite3.Connection, telegram: TelegramAdapter, *,
                      run_id: str, recipient_reference: str, now: datetime) -> DigestResult:
    """Freeze the digest before delivery so retries use exactly the same body."""
    if now.tzinfo is not timezone.utc:
        raise ValueError("now must be UTC")
    if not run_id.strip() or not recipient_reference.strip():
        raise ValueError("run ID and recipient are required")
    key = stable_key("notification", "telegram", recipient_reference, run_id, "radar-digest")
    saved = conn.execute(
        "SELECT body, idempotency_key, opportunity_ids_json FROM radar_digest_snapshots WHERE run_id = ? AND recipient_reference = ?",
        (run_id, recipient_reference),
    ).fetchone()
    if saved:
        body, saved_key, ids_json = saved
        if saved_key != key:
            raise ValueError("digest snapshot key conflicts with run and recipient")
        ids = tuple(json.loads(ids_json))
    else:
        rows = _new_candidates(conn, run_id)
        if not rows:
            return DigestResult("empty", None, (), None)
        while rows and len(_digest_text(rows)) > 4096:
            rows = rows[:-1]
        if not rows:
            raise ValueError("candidate URL is too long for a Telegram digest")
        body = _digest_text(rows)
        ids = tuple(row[0] for row in rows)
        # LiveTelegram uses its own SQLite connection; commit the snapshot first.
        with conn:
            conn.execute(
                """INSERT INTO radar_digest_snapshots
                   (run_id, recipient_reference, body, opportunity_ids_json, idempotency_key, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, recipient_reference, body, json.dumps(ids), key, to_storage(now)),
            )
    message_id = telegram.send_message(recipient_reference, body, idempotency_key=key)
    return DigestResult("sent", message_id, ids, body)
