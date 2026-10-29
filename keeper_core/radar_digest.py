"""Stable top-five Telegram digest for qualified opportunities."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from .adapters import TelegramAdapter
from .time import to_storage
from .workflow import stable_key


POLICY_VERSION = "eligible-score-7-v1"


@dataclass(frozen=True, slots=True)
class DigestResult:
    status: str  # sent, empty, review
    message_id: str | None
    opportunity_ids: tuple[str, ...]
    text: str | None


def _new_candidates(conn: sqlite3.Connection, recipient_reference: str) -> tuple[tuple, ...]:
    return tuple(conn.execute(
        """SELECT p.id, p.organization, p.title, COALESCE(o.observed_url, p.canonical_url),
                  p.score, p.explanation, p.eligibility
           FROM opportunities AS p
           JOIN opportunity_observations AS o ON o.opportunity_id = p.id
           WHERE p.score >= 7 AND p.eligibility = 'eligible'
             AND NOT EXISTS (
                 SELECT 1 FROM opportunity_observations AS newer
                 WHERE newer.opportunity_id = o.opportunity_id
                   AND (newer.observed_at > o.observed_at OR
                        (newer.observed_at = o.observed_at AND newer.run_id > o.run_id))
             )
             AND NOT EXISTS (
                 SELECT 1 FROM radar_digest_snapshots AS s,
                              json_each(s.opportunity_ids_json) AS reserved
                 WHERE s.recipient_reference = ? AND reserved.value = p.id
             )
           ORDER BY p.score DESC, p.organization, p.title LIMIT 5""",
        (recipient_reference,),
    ).fetchall())


def _one_line(value: str, limit: int) -> str:
    return " ".join(value.split())[:limit]


def _digest_text(rows: tuple[tuple, ...]) -> str:
    lines = ["Keeper opportunity radar — qualified matches"]
    for index, (_, organization, title, url, score, reason, eligibility) in enumerate(rows, 1):
        lines.append(
            f"{index}. {_one_line(organization, 70)} — {_one_line(title, 100)} "
            f"| {score}/10 | {eligibility}\n"
            f"   {_one_line(reason or 'Relevant role', 220)}\n   {url}"
        )
    return "\n".join(lines)


def _snapshot_is_safe(conn: sqlite3.Connection, ids_json: str) -> bool:
    return conn.execute(
        """SELECT 1 FROM json_each(?) AS ids
           LEFT JOIN opportunities AS p ON p.id = ids.value
           WHERE p.id IS NULL OR p.score IS NULL OR p.score < 7 OR p.eligibility != 'eligible'
           LIMIT 1""", (ids_json,),
    ).fetchone() is None


def send_radar_digest(conn: sqlite3.Connection, telegram: TelegramAdapter, *,
                      run_id: str, recipient_reference: str, now: datetime) -> DigestResult:
    """Freeze a qualified digest before delivery so retries reuse its exact body."""
    if now.tzinfo is not timezone.utc:
        raise ValueError("now must be UTC")
    if not run_id.strip() or not recipient_reference.strip():
        raise ValueError("run ID and recipient are required")
    key = stable_key("notification", "telegram", recipient_reference, run_id, "radar-digest")
    if conn.in_transaction:
        raise ValueError("digest delivery requires a connection without an active transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        saved = conn.execute(
            """SELECT body, idempotency_key, opportunity_ids_json, policy_version
               FROM radar_digest_snapshots WHERE run_id = ? AND recipient_reference = ?""",
            (run_id, recipient_reference),
        ).fetchone()
        if saved:
            body, saved_key, ids_json, policy_version = saved
            if saved_key != key:
                raise ValueError("digest snapshot key conflicts with run and recipient")
            ids = tuple(json.loads(ids_json))
            if policy_version != POLICY_VERSION or not _snapshot_is_safe(conn, ids_json):
                conn.commit()
                return DigestResult("review", None, ids, None)
        else:
            rows = _new_candidates(conn, recipient_reference)
            if not rows:
                conn.commit()
                return DigestResult("empty", None, (), None)
            while rows and len(_digest_text(rows)) > 4096:
                rows = rows[:-1]
            if not rows:
                raise ValueError("candidate URL is too long for a Telegram digest")
            body = _digest_text(rows)
            ids = tuple(row[0] for row in rows)
            conn.execute(
                """INSERT INTO radar_digest_snapshots
                   (run_id, recipient_reference, body, opportunity_ids_json,
                    idempotency_key, created_at, policy_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (run_id, recipient_reference, body, json.dumps(ids), key,
                 to_storage(now), POLICY_VERSION),
            )
        # Reserve candidates before releasing the write lock; send outside it.
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    message_id = telegram.send_message(recipient_reference, body, idempotency_key=key)
    return DigestResult("sent", message_id, ids, body)
