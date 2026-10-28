"""Cross-run opportunity identity and append-only radar observations."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .contracts import Opportunity
from .ingest import IdentityConflict, insert_opportunity_once
from .time import to_storage
from .workflow import canonical_url, opportunity_identity_key, opportunity_key


@dataclass(frozen=True, slots=True)
class PersistedRadarRun:
    new_ids: tuple[str, ...]
    known_ids: tuple[str, ...]
    resolved_ids: tuple[str, ...]


def persist_radar_run(conn: sqlite3.Connection, opportunities: tuple[Opportunity, ...],
                      *, run_id: str) -> PersistedRadarRun:
    """Retain first discovery and one immutable observation per posting per run.

    The caller owns the outer transaction. A savepoint prevents partial writes
    when any posting has a conflicting identity or reused run key.
    """
    if not run_id.strip():
        raise ValueError("run_id is required")
    new_ids: list[str] = []
    known_ids: list[str] = []
    resolved_ids: list[str] = []
    conn.execute("SAVEPOINT radar_run")
    try:
        for opportunity in opportunities:
            if canonical_url(opportunity.canonical_url) != opportunity.canonical_url:
                raise ValueError("opportunity URL must be canonical")
            if opportunity.dedupe_key != opportunity_key(
                opportunity.canonical_url, opportunity.organization, opportunity.title
            ):
                raise ValueError("opportunity dedupe key does not match its identity")
            rows = conn.execute("SELECT id, canonical_url, organization, title FROM opportunities").fetchall()
            by_url = [row for row in rows if row[1] == opportunity.canonical_url]
            identity = opportunity_identity_key(opportunity.organization, opportunity.title)
            by_role = [row for row in rows if opportunity_identity_key(row[2], row[3]) == identity]
            candidates = {row[0] for row in by_url + by_role}
            if len(candidates) > 1:
                raise IdentityConflict("canonical URL and organization/title identify different opportunities")
            if by_url and not by_role:
                raise IdentityConflict("canonical URL was previously assigned to another organization/title")
            if candidates:
                keeper_id = next(iter(candidates))
                known_ids.append(keeper_id)
            else:
                keeper_id = insert_opportunity_once(conn, opportunity)
                new_ids.append(keeper_id)
            resolved_ids.append(keeper_id)
            values = (
                keeper_id, run_id, opportunity.canonical_url, to_storage(opportunity.discovered_at),
                opportunity.source, opportunity.score, opportunity.explanation,
                opportunity.eligibility.value, opportunity.eligibility_reason,
            )
            old = conn.execute(
                """SELECT opportunity_id, run_id, observed_url, observed_at, source, score,
                          explanation, eligibility, eligibility_reason
                   FROM opportunity_observations WHERE opportunity_id = ? AND run_id = ?""",
                (keeper_id, run_id),
            ).fetchone()
            if old is not None:
                if old[2] != opportunity.canonical_url:
                    raise IdentityConflict("run ID was reused with a different posting URL")
                continue
            latest = conn.execute(
                "SELECT MAX(observed_at) FROM opportunity_observations WHERE opportunity_id = ?",
                (keeper_id,),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO opportunity_observations
                   (opportunity_id, run_id, observed_url, observed_at, source, score,
                    explanation, eligibility, eligibility_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", values,
            )
            if latest is None or values[3] >= latest:
                conn.execute(
                    """UPDATE opportunities SET score = ?, explanation = ?, eligibility = ?,
                       eligibility_reason = ? WHERE id = ?""",
                    (opportunity.score, opportunity.explanation, opportunity.eligibility.value,
                     opportunity.eligibility_reason, keeper_id),
                )
        conn.execute("RELEASE SAVEPOINT radar_run")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT radar_run")
        conn.execute("RELEASE SAVEPOINT radar_run")
        raise
    return PersistedRadarRun(tuple(new_ids), tuple(known_ids), tuple(resolved_ids))


def opportunity_history(conn: sqlite3.Connection, opportunity_id: str) -> tuple[tuple, ...]:
    """Return prior daily observations oldest first without modifying the record."""
    return tuple(conn.execute(
        """SELECT run_id, observed_url, observed_at, source, score, explanation,
                  eligibility, eligibility_reason
           FROM opportunity_observations WHERE opportunity_id = ?
           ORDER BY observed_at, run_id""", (opportunity_id,),
    ).fetchall())
