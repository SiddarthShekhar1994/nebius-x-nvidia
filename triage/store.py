"""Immutable saved decisions; caller must save successfully before provider changes."""
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from keeper_core.adapters import IdempotencyConflict
from keeper_core.contracts import EmailRecord, IngestionMetadata, ModelMetadata, TriageBucket, TriageResult
from keeper_core.db import migrate
from keeper_core.ingest import insert_email_once
from keeper_core.time import from_storage, to_storage


def _result(row):
    model = ModelMetadata(row["requested_model"], row["served_model"], row["prompt_version"],
                          row["router_call_id"]) if row["requested_model"] else None
    return TriageResult(
        id=row["id"], email_id=row["email_id"],
        bucket=TriageBucket(row["bucket"]) if row["bucket"] else None,
        reason=row["reason"], career=bool(row["career"]) if row["career"] is not None else None,
        scam_signals=tuple(json.loads(row["scam_signals_json"])),
        extracted_deadline=from_storage(row["extracted_deadline"]),
        deadline_evidence=row["deadline_evidence"], confidence=row["confidence"],
        model=model, classified_at=from_storage(row["classified_at"]),
    )


def _email(row):
    return EmailRecord(
        id=row["id"], provider=row["provider"], provider_id=row["provider_id"], thread_id=row["thread_id"],
        sender=row["sender"], subject=row["subject"], received_at=from_storage(row["received_at"]),
        body_excerpt=row["body_excerpt"], labels=tuple(json.loads(row["labels_json"])),
        raw_link_reference=row["raw_link_reference"],
        ingestion=IngestionMetadata(from_storage(row["ingested_at"]), row["ingestion_run_id"], row["ingestion_source"]),
    )


def saved_decisions(path):
    """Every saved (email, triage result) pair, oldest email first. Read-only."""
    path = Path(path)
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        emails = {row["id"]: _email(row) for row in conn.execute("SELECT * FROM email_records")}
        rows = conn.execute(
            "SELECT t.* FROM triage_results t JOIN email_records e ON e.id = t.email_id "
            "ORDER BY e.received_at, e.id"
        ).fetchall()
    return [(emails[row["email_id"]], _result(row)) for row in rows]


@dataclass(frozen=True)
class ResultStore:
    path: Path
    account: str | None = None

    def _binding(self, conn, *, create=False):
        if self.account is None:
            return
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='triage_account'").fetchone()
        bound = conn.execute("SELECT account FROM triage_account WHERE singleton=1").fetchone() if exists else None
        if bound and bound[0] != self.account.casefold():
            raise IdempotencyConflict("Database belongs to another Gmail account")
        if bound is None:
            old = conn.execute(
                "SELECT 1 FROM triage_results t JOIN email_records e ON e.id=t.email_id "
                "WHERE e.provider='gmail' LIMIT 1"
            ).fetchone()
            if old:
                raise IdempotencyConflict("Unbound Gmail decisions require operator review")
            if create:
                conn.execute("INSERT INTO triage_account(singleton,account) VALUES(1,?)", (self.account.casefold(),))

    @staticmethod
    def _get(conn, email_id):
        cursor = conn.execute("SELECT * FROM triage_results WHERE email_id=?", (email_id,))
        cursor.row_factory = sqlite3.Row
        row = cursor.fetchone()
        if row is None:
            return None
        corrected = conn.execute(
            "SELECT 1 FROM correction_records WHERE target_type='triage' "
            "AND target_id IN (?,?) AND approval_status='approved' LIMIT 1", (row["id"], email_id),
        ).fetchone()
        if corrected:
            raise IdempotencyConflict("Approved correction requires manual reconciliation")
        return _result(row)

    def get(self, email_id, *, email=None):
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            self._binding(conn)
            result = self._get(conn, email_id)
            if result is not None and email is not None:
                self._verify_source(conn, email)
            return result

    @staticmethod
    def _verify_source(conn, email):
        row = conn.execute(
            "SELECT provider,provider_id,sender,subject,received_at,body_excerpt FROM email_records WHERE id=?",
            (email.id,),
        ).fetchone()
        expected = (email.provider, email.provider_id, email.sender, email.subject,
                    email.received_at, email.body_excerpt)
        actual = (*row[:4], from_storage(row[4]), row[5]) if row else None
        if actual != expected:
            raise IdempotencyConflict("Saved source changed; reconcile before reclassifying")

    def save(self, email, result):
        if result.email_id != email.id:
            raise ValueError("Result belongs to another email")
        if email.provider == "gmail" and not self.account:
            raise ValueError("Saving Gmail results requires an account binding")
        migrate(self.path)
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE")
            self._binding(conn, create=True)
            existing = self._get(conn, email.id)
            if existing is not None:
                if existing != result:
                    raise IdempotencyConflict("Saved decision cannot be overwritten automatically")
                return
            insert_email_once(conn, email)
            model = result.model
            conn.execute(
                """INSERT INTO triage_results
                   (id,email_id,bucket,reason,career,scam_signals_json,extracted_deadline,
                    deadline_evidence,confidence,requested_model,served_model,prompt_version,
                    router_call_id,classified_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (result.id, email.id, result.bucket.value if result.bucket else None, result.reason,
                 result.career, json.dumps(result.scam_signals), to_storage(result.extracted_deadline),
                 result.deadline_evidence, result.confidence, model.requested_model if model else None,
                 model.served_model if model else None, model.prompt_version if model else None,
                 model.router_call_id if model else None, to_storage(result.classified_at)),
            )
