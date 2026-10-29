"""Offline import and immutable storage for human-reviewed posting evidence."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import Eligibility, Opportunity
from .radar import RadarProfile
from .radar_deadlines import VerifiedPostingDeadline
from .radar_enrichment import VerifiedPostingRequirements, check_eligibility
from .time import from_storage, to_storage
from .workflow import canonical_url


_MAX_FILE_BYTES = 64 * 1024
_MAX_EXCERPT_BYTES = 8 * 1024
_TOP_FIELDS = {"opportunity_id", "source_url", "source_excerpt", "requirements", "deadline_timestamp"}
_REQUIREMENT_FIELDS = {
    "complete", "graduation_years", "majors", "locations", "remote_only",
    "relocation_required", "authorization",
}


@dataclass(frozen=True, slots=True)
class PostingEvidence:
    opportunity_id: str
    source_url: str
    source_excerpt: str
    requirements: VerifiedPostingRequirements
    deadline: VerifiedPostingDeadline | None

    def __post_init__(self) -> None:
        source_url = canonical_url(self.source_url)
        if source_url != self.source_url or self.requirements.source_url != source_url:
            raise ValueError("posting evidence URLs must be canonical and match")
        if self.requirements.evidence != self.source_excerpt:
            raise ValueError("posting evidence excerpt must match requirements evidence")
        if self.deadline is not None and (
            self.deadline.source_url != source_url or self.deadline.source_excerpt != self.source_excerpt
        ):
            raise ValueError("posting evidence deadline must use the same source page and excerpt")


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _object(value: Any, field: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} keys must be strings")
    extra = set(value) - allowed
    if extra:
        raise ValueError(f"unsupported {field} fields: {', '.join(sorted(extra))}")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _parse_list(value: Any, field: str, *, integers: bool = False) -> tuple:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field} must be a nonempty list")
    if integers:
        if any(type(item) is not int for item in value):
            raise ValueError(f"{field} must contain integers")
    elif any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must contain nonempty strings")
    return tuple(value)


def parse_posting_evidence(value: dict[str, Any]) -> PostingEvidence:
    """Parse a human-reviewed JSON object without fetching or inferring evidence."""
    data = _object(value, "evidence", _TOP_FIELDS)
    if set(data) != _TOP_FIELDS:
        raise ValueError(f"evidence fields required: {', '.join(sorted(_TOP_FIELDS))}")
    opportunity_id = _text(data["opportunity_id"], "opportunity_id")
    source_url = canonical_url(_text(data["source_url"], "source_url"))
    source_excerpt = _text(data["source_excerpt"], "source_excerpt")
    if len(source_excerpt.encode("utf-8")) > _MAX_EXCERPT_BYTES:
        raise ValueError("source_excerpt exceeds 8 KiB")

    raw_requirements = _object(data["requirements"], "requirements", _REQUIREMENT_FIELDS)
    if "complete" not in raw_requirements or type(raw_requirements["complete"]) is not bool:
        raise ValueError("requirements.complete must be a boolean")
    kwargs: dict[str, Any] = {"source_url": source_url, "evidence": source_excerpt,
                              "complete": raw_requirements["complete"]}
    for key in ("graduation_years", "majors", "locations"):
        if key in raw_requirements:
            value = raw_requirements[key]
            kwargs[key] = None if value is None else _parse_list(
                value, f"requirements.{key}", integers=(key == "graduation_years"))
    for key in ("remote_only", "relocation_required"):
        if key in raw_requirements:
            value = raw_requirements[key]
            if value is not None and type(value) is not bool:
                raise ValueError(f"requirements.{key} must be a boolean or null")
            kwargs[key] = value
    if "authorization" in raw_requirements:
        value = raw_requirements["authorization"]
        if value is not None and not isinstance(value, str):
            raise ValueError("requirements.authorization must be a string or null")
        kwargs["authorization"] = value
    requirements = VerifiedPostingRequirements(**kwargs)

    timestamp = data["deadline_timestamp"]
    deadline = None
    if timestamp is not None:
        timestamp = _text(timestamp, "deadline_timestamp")
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("deadline_timestamp must be an explicit ISO timestamp") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("deadline_timestamp must include an explicit timezone")
        deadline = VerifiedPostingDeadline(source_url, source_excerpt, timestamp,
                                           parsed.astimezone(timezone.utc))
    return PostingEvidence(opportunity_id, source_url, source_excerpt, requirements, deadline)


def load_posting_evidence(path: Path) -> PostingEvidence:
    """Load a bounded UTF-8 JSON review file; duplicate keys are rejected."""
    with Path(path).open("rb") as source:
        raw = source.read(_MAX_FILE_BYTES + 1)
    if len(raw) > _MAX_FILE_BYTES:
        raise ValueError("posting evidence file exceeds 64 KiB")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("posting evidence must be valid UTF-8 JSON") from exc
    return parse_posting_evidence(value)


def _stored_opportunity(conn: sqlite3.Connection, evidence: PostingEvidence) -> tuple[Opportunity, str]:
    row = conn.execute(
        """SELECT id, dedupe_key, canonical_url, organization, title, source,
                  discovered_at, score, explanation, eligibility, eligibility_reason,
                  deadline, deadline_evidence
           FROM opportunities WHERE id = ?""", (evidence.opportunity_id,),
    ).fetchone()
    if row is None:
        raise ValueError("opportunity does not exist")
    if canonical_url(row[2]) != row[2]:
        raise ValueError("stored opportunity URL is not canonical")
    source_url = canonical_url(evidence.source_url)
    observed = conn.execute(
        """SELECT 1 FROM opportunity_observations
           WHERE opportunity_id = ? AND observed_url = ? LIMIT 1""",
        (evidence.opportunity_id, source_url),
    ).fetchone()
    if source_url != row[2] and observed is None:
        raise ValueError("source URL is not recorded for this opportunity")
    opportunity = Opportunity(
        row[0], row[1], row[2], row[3], row[4], row[5], from_storage(row[6]), row[7], row[8],
        Eligibility(row[9]), row[10], from_storage(row[11]), row[12],
    )
    return opportunity, source_url


def preview_posting_review(conn: sqlite3.Connection, profile: RadarProfile, evidence: PostingEvidence) -> Opportunity:
    """Return the eligibility result without writing any database state."""
    opportunity, source_url = _stored_opportunity(conn, evidence)
    reviewed_page = replace(opportunity, canonical_url=source_url)
    return check_eligibility(profile, reviewed_page, evidence.requirements)


def _requirements_json(value: VerifiedPostingRequirements) -> dict[str, Any]:
    return {
        "complete": value.complete,
        "graduation_years": value.graduation_years,
        "majors": value.majors,
        "locations": value.locations,
        "remote_only": value.remote_only,
        "relocation_required": value.relocation_required,
        "authorization": value.authorization,
    }


def _evidence_json(evidence: PostingEvidence) -> str:
    return json.dumps({
        "opportunity_id": evidence.opportunity_id,
        "source_url": canonical_url(evidence.source_url),
        "source_excerpt": evidence.source_excerpt,
        "requirements": _requirements_json(evidence.requirements),
        "deadline_timestamp": evidence.deadline.raw_timestamp if evidence.deadline else None,
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _review_dict(row: tuple) -> dict[str, Any]:
    evidence = json.loads(row[2])
    return {
        "id": row[0], "opportunity_id": row[1], "source_url": evidence["source_url"],
        "source_excerpt": evidence["source_excerpt"], "requirements": evidence["requirements"],
        "deadline_timestamp": evidence["deadline_timestamp"], "reviewed_by": row[3],
        "reviewed_at": from_storage(row[4]), "eligibility": Eligibility(row[5]),
        "eligibility_reason": row[6], "approved_by": row[7],
        "approved_at": from_storage(row[8]), "approval_reason": row[9],
    }


_REVIEW_SELECT = """SELECT id, opportunity_id, evidence_json, reviewed_by, reviewed_at,
                          eligibility, eligibility_reason, approved_by, approved_at, approval_reason
                   FROM posting_reviews"""


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or any(
        ord(char) < 32 or ord(char) == 127 for char in value
    ) or len(value) > 200:
        raise ValueError(f"{field} must be a nonempty identifier of at most 200 characters")
    return value


def save_posting_review(conn: sqlite3.Connection, profile: RadarProfile, evidence: PostingEvidence, *,
                        review_id: str, reviewed_by: str, now: datetime) -> dict[str, Any]:
    """Persist an immutable review; caller owns the surrounding transaction."""
    review_id = _identifier(review_id, "review_id")
    reviewed_by = _identifier(reviewed_by, "reviewed_by")
    if now.tzinfo is not timezone.utc:
        raise ValueError("now must be UTC")
    result = preview_posting_review(conn, profile, evidence)
    evidence_json = _evidence_json(evidence)
    existing = conn.execute(_REVIEW_SELECT + " WHERE id = ?", (review_id,)).fetchone()
    if existing is not None:
        if (existing[1], existing[2], existing[3], existing[5], existing[6]) != (
            evidence.opportunity_id, evidence_json, reviewed_by,
            result.eligibility.value, result.eligibility_reason,
        ):
            raise ValueError("review ID was already used for different evidence")
        return _review_dict(existing)

    latest = conn.execute(
        """SELECT reviewed_at, rowid FROM posting_reviews WHERE opportunity_id = ?
           ORDER BY reviewed_at DESC, rowid DESC LIMIT 1""", (evidence.opportunity_id,),
    ).fetchone()
    stamp = to_storage(now)
    if latest is not None and stamp < latest[0]:
        raise ValueError("new review cannot predate the latest review")
    conn.execute(
        """INSERT INTO posting_reviews
           (id, opportunity_id, evidence_json, reviewed_by, reviewed_at, eligibility, eligibility_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (review_id, evidence.opportunity_id, evidence_json, reviewed_by, stamp,
         result.eligibility.value, result.eligibility_reason or ""),
    )
    current_rowid = conn.execute("SELECT rowid FROM posting_reviews WHERE id = ?", (review_id,)).fetchone()[0]
    if latest is None or stamp > latest[0] or (stamp == latest[0] and current_rowid > latest[1]):
        conn.execute(
            "UPDATE opportunities SET eligibility = ?, eligibility_reason = ? WHERE id = ?",
            (result.eligibility.value, result.eligibility_reason, evidence.opportunity_id),
        )
    row = conn.execute(_REVIEW_SELECT + " WHERE id = ?", (review_id,)).fetchone()
    return _review_dict(row)


def get_posting_review(conn: sqlite3.Connection, review_id: str) -> dict[str, Any] | None:
    row = conn.execute(_REVIEW_SELECT + " WHERE id = ?", (review_id,)).fetchone()
    return None if row is None else _review_dict(row)


def list_posting_reviews(conn: sqlite3.Connection, opportunity_id: str | None = None) -> list[dict[str, Any]]:
    if opportunity_id is None:
        rows = conn.execute(_REVIEW_SELECT + " ORDER BY reviewed_at, rowid").fetchall()
    else:
        rows = conn.execute(_REVIEW_SELECT + " WHERE opportunity_id = ? ORDER BY reviewed_at, rowid",
                            (opportunity_id,)).fetchall()
    return [_review_dict(row) for row in rows]


def reviewed_requirements(conn: sqlite3.Connection) -> dict[str, VerifiedPostingRequirements]:
    """Map every recorded URL for each opportunity to its newest source review."""
    rows = conn.execute(
        """SELECT r.opportunity_id, r.evidence_json FROM posting_reviews r
           WHERE NOT EXISTS (
               SELECT 1 FROM posting_reviews newer
               WHERE newer.opportunity_id = r.opportunity_id
                 AND (newer.reviewed_at > r.reviewed_at
                      OR (newer.reviewed_at = r.reviewed_at AND newer.rowid > r.rowid))
           )"""
    ).fetchall()
    result = {}
    owners = {}
    for opportunity_id, serialized in rows:
        value = json.loads(serialized)
        requirements = parse_posting_evidence(value).requirements
        urls = {row[0] for row in conn.execute(
            "SELECT canonical_url FROM opportunities WHERE id = ?", (opportunity_id,),
        )}
        urls.update(row[0] for row in conn.execute(
            "SELECT observed_url FROM opportunity_observations WHERE opportunity_id = ?",
            (opportunity_id,),
        ))
        for url in urls:
            if url in owners and owners[url] != opportunity_id:
                raise ValueError("one recorded URL belongs to multiple reviewed opportunities")
            previous = result.get(url)
            if previous is not None and previous != requirements:
                raise ValueError("one recorded URL belongs to multiple reviewed opportunities")
            owners[url] = opportunity_id
            result[url] = requirements
    return result
