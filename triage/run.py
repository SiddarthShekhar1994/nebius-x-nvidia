"""Save a stable decision before additive labels; reuse it on every retry."""
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone

from keeper_core.adapters import AdapterError, AuthenticationError, IdempotencyConflict, ProviderUnavailable
from keeper_core.application_activity import activity_for_email
from keeper_core.workflow import stable_key
from .classifier import MODEL_UNAVAILABLE_REASON, _review, classify_email
from .model import ModelConfigurationError
from .preview import MAX_BODY_CHARS, _serialize


def run_triage(source, model, store, *, save=False, apply=False, limit=1000,
               classified_at=None, require_live=False):
    if not 1 <= limit <= 1000 or (apply and (not save or not source.can_apply)):
        raise ValueError("Apply requires saved decisions and a verified Gmail source")
    now = classified_at or datetime.now(timezone.utc)
    rows, errors = [], []
    identities = source.identities(limit)
    fatal = None
    for identity in identities:
        try:
            row = _process(source, identity, model, store, now, save, apply, require_live)
            rows.append(row)
        except (AuthenticationError, ModelConfigurationError) as exc:
            fatal = type(exc).__name__
            errors.append({"email_id": identity, "fixture_id": source.fixture_id(identity), "error_type": fatal})
            break
        except (AdapterError, OSError, ValueError, sqlite3.Error) as exc:
            errors.append({"email_id": identity, "fixture_id": source.fixture_id(identity), "error_type": type(exc).__name__})
    counts = Counter(row["bucket"] or "review" for row in rows)
    return {
        "operation": "apply" if apply else ("save" if save else "dry-run"),
        "fatal_error": fatal,
        "summary": {
            **{name: counts[name] for name in ("junk", "actionable", "due", "newsletter", "review")},
            "total": len(identities), "failed": len(errors), "unprocessed": len(identities) - len(rows) - len(errors),
            "applied": sum(row["status"] == "applied" for row in rows),
            "reused": sum(row["reused"] for row in rows),
            "calendar_lines": sum(line["kind"].startswith("calendar_")
                                  for row in rows for line in row["activity_lines"]),
            "draft_lines": sum(line["kind"] == "gmail_draft"
                               for row in rows for line in row["activity_lines"]),
        },
        "results": rows, "errors": errors,
    }


def _process(source, identity, model, store, now, save, apply, require_live):
    record, body, fixture_id = source.read(identity)
    result = store.get(record.id, email=record)
    reused = result is not None
    if reused and require_live and (
        result.model is not None and result.model.requested_model == "offline-demo"
    ):
        raise IdempotencyConflict("Stored offline result cannot authorize live actions")
    if result is None:
        bounded = replace(record, body_excerpt=body[:MAX_BODY_CHARS])
        result = (
            _review(bounded, now, None, "Needs review: message exceeds classification size limit.")
            if len(body) > MAX_BODY_CHARS else classify_email(bounded, model, classified_at=now)
        )
    if result.reason == MODEL_UNAVAILABLE_REASON:
        raise ProviderUnavailable("Model temporarily unavailable; retry without a frozen decision")
    row = {**_serialize(result), "fixture_id": fixture_id, "reused": reused, "status": "dry-run"}
    if save:
        store.save(record, result)
        row = {**row, "status": "saved"}
    if apply:
        store.get(record.id)  # Check for a newly approved manual correction before applying.
        source.apply_labels(record.id, tuple(row["proposed_labels"]),
                            idempotency_key=stable_key("triage-labels", source.account, result.id))
        row = {**row, "status": "applied"}
    row["activity_lines"] = _activity_lines(store, record.id)
    return row


def _activity_lines(store, email_id):
    if not store.path.is_file():
        return []
    with closing(sqlite3.connect(store.path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_activity'"
        ).fetchone()
        return activity_for_email(conn, email_id) if exists else []
