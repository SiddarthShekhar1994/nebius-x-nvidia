"""SQLite application repository and conservative message identity resolution.

The caller owns the connection and commit. Enable SQLite foreign keys on it.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from .contracts import (
    Application, ApplicationStatus, ApplicationStatusChange, CorrectionRecord, ManualOverride,
)
from .time import from_storage, to_storage
from .workflow import (
    TransitionConflict, advance_application, opportunity_identity_key, organization_key, stable_key,
)


@dataclass(frozen=True, slots=True)
class IdentityResolution:
    status: str  # matched, review, unmatched
    application_id: str | None
    candidate_application_ids: tuple[str, ...]
    reason: str


@contextmanager
def _atomic(conn: sqlite3.Connection):
    """Keep a multi-table repository write atomic without committing caller work."""
    conn.execute("SAVEPOINT keeper_application_write")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK TO keeper_application_write")
        conn.execute("RELEASE keeper_application_write")
        raise
    else:
        conn.execute("RELEASE keeper_application_write")


def resolve_message_application(
    conn: sqlite3.Connection, *, organization: str | None, title: str | None,
) -> IdentityResolution:
    """Match a message only when both normalized names identify one opportunity.

    A missing title, multiple matching postings, or a title not known for a
    known organization always needs review. No fuzzy or subject-only match is made.
    """
    if not organization or not organization.strip():
        return IdentityResolution("review", None, (), "organization is missing")
    org_key = organization_key(organization)
    rows = conn.execute(
        """SELECT o.id, o.organization, o.title, a.id
           FROM opportunities o LEFT JOIN applications a ON a.opportunity_id = o.id
           ORDER BY o.id"""
    ).fetchall()
    same_org = [row for row in rows if organization_key(row[1]) == org_key]
    if not same_org:
        return IdentityResolution("unmatched", None, (), "organization is not tracked")
    candidates = tuple(row[3] for row in same_org if row[3] is not None)
    if not title or not title.strip():
        return IdentityResolution("review", None, candidates, "opportunity title is missing")
    identity = opportunity_identity_key(organization, title)
    matches = [row for row in same_org if opportunity_identity_key(row[1], row[2]) == identity]
    if len(matches) != 1:
        reason = "multiple opportunities share this organization and title" if matches else "title does not match a tracked opportunity"
        return IdentityResolution("review", None, tuple(row[3] for row in matches if row[3]), reason)
    application_id = matches[0][3]
    if application_id is None:
        return IdentityResolution("review", None, (), "matched opportunity has no application")
    return IdentityResolution("matched", application_id, (application_id,), "unique organization and title match")


def _read_application(conn: sqlite3.Connection, row: tuple) -> Application:
    app_id, opportunity_id, status, created_at, updated_at, actor, override_reason, override_at, outcome = row
    changes = conn.execute(
        """SELECT id, status, changed_at, source_email_id, reason, manual,
                  source_evidence, correction_id, decision_outcome
           FROM application_status_changes WHERE application_id = ?
           ORDER BY changed_at, rowid""", (app_id,)
    ).fetchall()
    history = tuple(ApplicationStatusChange(
        change_id, ApplicationStatus(change_status), from_storage(at), email_id,
        reason, bool(manual), evidence, correction_id, decision_outcome,
    ) for change_id, change_status, at, email_id, reason, manual, evidence, correction_id, decision_outcome in changes)
    email_ids = tuple(row[0] for row in conn.execute(
        "SELECT email_id FROM application_source_emails WHERE application_id = ? ORDER BY rowid", (app_id,)
    ))
    override = ManualOverride(actor, override_reason, from_storage(override_at)) if actor else None
    return Application(app_id, opportunity_id, ApplicationStatus(status), history, email_ids,
                       from_storage(created_at), from_storage(updated_at), override, outcome)


def get_application(conn: sqlite3.Connection, application_id: str) -> Application | None:
    row = conn.execute(
        """SELECT id, opportunity_id, status, created_at, updated_at,
                  manual_override_actor_reference, manual_override_reason,
                  manual_override_applied_at, decision_outcome
           FROM applications WHERE id = ?""", (application_id,)
    ).fetchone()
    return _read_application(conn, row) if row else None


def list_applications(conn: sqlite3.Connection) -> tuple[Application, ...]:
    ids = conn.execute("SELECT id FROM applications ORDER BY created_at, id").fetchall()
    return tuple(get_application(conn, row[0]) for row in ids)


def create_application(
    conn: sqlite3.Connection, opportunity_id: str, *, created_at: datetime,
    source_evidence: str,
) -> Application:
    """Create one found application with an auditable opportunity source."""
    if not source_evidence.strip():
        raise ValueError("source evidence is required")
    stamp = to_storage(created_at)
    existing = conn.execute("SELECT id FROM applications WHERE opportunity_id = ?", (opportunity_id,)).fetchone()
    if existing:
        return get_application(conn, existing[0])
    if not conn.execute("SELECT 1 FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone():
        raise ValueError("opportunity does not exist")
    app_id = stable_key("application", opportunity_id)
    change_id = stable_key("application-change", app_id, "created")
    with _atomic(conn):
        conn.execute(
            "INSERT INTO applications(id, opportunity_id, status, created_at, updated_at) VALUES (?, ?, 'found', ?, ?)",
            (app_id, opportunity_id, stamp, stamp),
        )
        conn.execute(
            """INSERT INTO application_status_changes
               (id, application_id, status, changed_at, reason, source_evidence)
               VALUES (?, ?, 'found', ?, 'Opportunity discovered', ?)""",
            (change_id, app_id, stamp, source_evidence),
        )
    return get_application(conn, app_id)


def apply_application_transition(
    conn: sqlite3.Connection, application_id: str, status: ApplicationStatus, *,
    changed_at: datetime, reason: str, source_evidence: str,
    source_email_id: str | None = None, correction: CorrectionRecord | None = None,
    decision_outcome: str | None = None,
) -> Application:
    """Persist a workflow transition and its evidence in the caller's transaction."""
    if not source_evidence.strip():
        raise ValueError("source evidence is required")
    current = get_application(conn, application_id)
    if current is None:
        raise ValueError("application does not exist")
    updated = advance_application(
        current, status, changed_at=changed_at, reason=reason,
        source_email_id=source_email_id, correction=correction,
        decision_outcome=decision_outcome, source_evidence=source_evidence,
    )
    if updated is current:
        return current
    if correction is not None:
        stored = conn.execute(
            """SELECT target_type, target_id, correction, approval_status,
                      created_at, approved_at, actor_reference
               FROM correction_records WHERE id = ?""", (correction.id,)
        ).fetchone()
        if stored != (
            "application", application_id, correction.correction, "approved",
            to_storage(correction.created_at), to_storage(changed_at), correction.actor_reference,
        ):
            raise ValueError("approved correction must be persisted before applying it")
    override = updated.manual_override
    with _atomic(conn):
        conn.execute(
            """UPDATE applications SET status = ?, updated_at = ?,
               manual_override_actor_reference = ?, manual_override_reason = ?,
               manual_override_applied_at = ?, decision_outcome = ? WHERE id = ?""",
            (updated.status.value, to_storage(updated.updated_at),
             override.actor_reference if override else None,
             override.reason if override else None,
             to_storage(override.applied_at) if override else None,
             updated.decision_outcome, application_id),
        )
        for change in updated.status_history[len(current.status_history):]:
            conn.execute(
                """INSERT INTO application_status_changes
                   (id, application_id, status, changed_at, source_email_id, reason,
                    manual, source_evidence, correction_id, decision_outcome)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (change.id, application_id, change.status.value, to_storage(change.changed_at),
                 change.source_email_id, change.reason, int(change.manual),
                 change.source_evidence, change.correction_id, change.decision_outcome),
            )
        for email_id in updated.source_email_ids:
            conn.execute(
                "INSERT OR IGNORE INTO application_source_emails(application_id, email_id) VALUES (?, ?)",
                (application_id, email_id),
            )
    return updated


def apply_manual_correction(
    conn: sqlite3.Connection, application_id: str, status: ApplicationStatus, *,
    correction_id: str, actor_reference: str, reason: str,
    source_evidence: str, approved_at: datetime,
    decision_outcome: str | None = None,
) -> Application:
    """Persist one explicitly approved correction and transition atomically.

    The stable correction ID makes retries idempotent. Reusing it with different
    content is a conflict, even after a later correction changed current state.
    """
    if not all(value.strip() for value in (correction_id, actor_reference, reason, source_evidence)):
        raise ValueError("correction ID, actor, reason, and evidence are required")
    if (status is ApplicationStatus.DECISION) != (decision_outcome is not None):
        raise ValueError("decision outcome is required only for decision status")
    if get_application(conn, application_id) is None:
        raise ValueError("application does not exist")
    existing = conn.execute(
        """SELECT target_type, target_id, correction, approval_status,
                  actor_reference FROM correction_records WHERE id = ?""",
        (correction_id,),
    ).fetchone()
    if existing:
        prior = conn.execute(
            """SELECT status, reason, source_evidence, decision_outcome
               FROM application_status_changes WHERE correction_id = ?""",
            (correction_id,),
        ).fetchone()
        if existing != ("application", application_id, reason, "approved", actor_reference) or prior != (
            status.value, reason, source_evidence, decision_outcome,
        ):
            raise TransitionConflict("correction ID was already used with different details")
        return get_application(conn, application_id)
    correction = CorrectionRecord(
        correction_id, "application", application_id, reason,
        approved_at, "approved", approved_at=approved_at,
        actor_reference=actor_reference,
    )
    with _atomic(conn):
        conn.execute(
            """INSERT INTO correction_records
               (id, target_type, target_id, correction, created_at, approval_status,
                approved_at, actor_reference)
               VALUES (?, 'application', ?, ?, ?, 'approved', ?, ?)""",
            (correction.id, application_id, correction.correction,
             to_storage(correction.created_at), to_storage(correction.approved_at),
             correction.actor_reference),
        )
        updated = apply_application_transition(
            conn, application_id, status, changed_at=approved_at,
            reason=reason, source_evidence=source_evidence,
            correction=correction, decision_outcome=decision_outcome,
        )
    return updated


def apply_message_transition(
    conn: sqlite3.Connection, *, organization: str | None, title: str | None,
    status: ApplicationStatus, changed_at: datetime, reason: str,
    source_evidence: str, source_email_id: str,
    decision_outcome: str | None = None,
) -> tuple[IdentityResolution, Application | None]:
    """Resolve identity before an automatic email update; review leaves state intact."""
    resolution = resolve_message_application(conn, organization=organization, title=title)
    if resolution.status != "matched":
        return resolution, None
    application = apply_application_transition(
        conn, resolution.application_id, status, changed_at=changed_at,
        reason=reason, source_evidence=source_evidence,
        source_email_id=source_email_id, decision_outcome=decision_outcome,
    )
    return resolution, application
