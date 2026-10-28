"""Append-only source-backed Calendar and Gmail draft activity."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .time import to_storage
from .workflow import stable_key


def record_activity(
    conn: sqlite3.Connection, *, application_id: str, kind: str,
    source_email_id: str, source_evidence: str, related_id: str,
    provider_id: str, detail: str, occurred_at: datetime,
    review_flags: tuple[str, ...] = (),
) -> str:
    if not all((application_id, source_email_id, source_evidence.strip(), related_id,
                provider_id, detail.strip())):
        raise ValueError("activity requires application, source evidence, and provider identity")
    activity_id = stable_key("application-activity", application_id, kind,
                             source_email_id, related_id)
    expected = (application_id, kind, source_email_id, source_evidence,
                related_id, provider_id, detail, json.dumps(review_flags))
    row = conn.execute(
        """SELECT application_id, kind, source_email_id, source_evidence,
                  related_id, provider_id, detail, review_flags_json
           FROM application_activity WHERE id = ?""", (activity_id,),
    ).fetchone()
    if row is not None:
        if row != expected:
            raise ValueError("activity retry conflicts with saved evidence")
        return activity_id
    conn.execute(
        """INSERT INTO application_activity
           (id, application_id, kind, source_email_id, source_evidence,
            related_id, provider_id, detail, review_flags_json, occurred_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (activity_id, *expected, to_storage(occurred_at)),
    )
    return activity_id


def activity_for_application(conn: sqlite3.Connection, application_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT id, kind, source_email_id, source_evidence, related_id,
                  provider_id, detail, review_flags_json, occurred_at
           FROM application_activity WHERE application_id = ?
           ORDER BY occurred_at, rowid""", (application_id,),
    )
    return [
        {"id": row[0], "kind": row[1], "source_email_id": row[2],
         "source_evidence": row[3], "related_id": row[4],
         "provider_id": row[5], "detail": row[6],
         "review_flags": json.loads(row[7]), "occurred_at": row[8]}
        for row in rows
    ]


def activity_for_email(conn: sqlite3.Connection, email_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT application_id, kind, detail, provider_id, review_flags_json,
                  source_evidence
           FROM application_activity WHERE source_email_id = ? ORDER BY occurred_at, rowid""",
        (email_id,),
    )
    return [{"application_id": row[0], "kind": row[1], "detail": row[2],
             "provider_id": row[3], "review_flags": json.loads(row[4]),
             "source_evidence": row[5]} for row in rows]
