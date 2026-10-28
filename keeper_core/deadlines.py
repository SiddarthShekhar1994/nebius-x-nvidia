"""Source-backed deadline candidates and an explicit calendar review gate."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .contracts import EmailRecord, TriageBucket, TriageResult
from .workflow import stable_key


class DeadlineReviewRequired(ValueError):
    """A candidate cannot be used for a calendar event without user review."""


@dataclass(frozen=True, slots=True)
class DeadlineCandidate:
    id: str
    source_email_id: str
    source_field: str
    source_reference: str
    raw_date: str
    evidence: str
    deadline_at: datetime | None
    flags: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.id or not self.source_email_id or not self.evidence.strip():
            raise ValueError("deadline candidate needs identity and source evidence")
        if self.deadline_at is not None and self.deadline_at.tzinfo is not timezone.utc:
            raise ValueError("candidate deadline must be UTC")


@dataclass(frozen=True, slots=True)
class DeadlineReviewPolicy:
    candidate_id: str
    approved_by: str
    reason: str
    approved_at: datetime
    confirmed_at: datetime
    allow_past: bool = False

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or not self.approved_by.strip() or not self.reason.strip():
            raise ValueError("review policy needs a candidate, approver, and reason")
        if self.approved_at.tzinfo is not timezone.utc or self.confirmed_at.tzinfo is not timezone.utc:
            raise ValueError("review policy timestamps must be UTC")
        if type(self.allow_past) is not bool:
            raise ValueError("allow_past must be boolean")


_CUE = re.compile(
    r"\b(?:deadline|due|apply by|submit by|applications? clos(?:e|es)|"
    r"rsvp by|respond by)\b", re.IGNORECASE,
)
_EXPLICIT = re.compile(
    r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:\d{2})\b",
)
_LOCAL = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?\b")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NATURAL = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+\d{1,2}(?:,\s*\d{4})?(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:AM|PM))?"
    r"(?:\s+(?:Pacific|Eastern|Central|Mountain|UTC|PST|PDT|EST|EDT))?\b",
    re.IGNORECASE,
)
_IANA = re.compile(r"^\s+([A-Za-z_]+/[A-Za-z_]+)")
_VAGUE_ZONE = re.compile(r"^\s+(?:PST|PDT|EST|EDT|Pacific|Eastern|Central|Mountain|local time)\b", re.IGNORECASE)
_VAGUE_ZONE_ANYWHERE = re.compile(
    r"\b(?:PST|PDT|EST|EDT|Pacific|Eastern|Central|Mountain|local time)\b", re.IGNORECASE,
)


def _utc(value: datetime, name: str) -> None:
    if value.tzinfo is not timezone.utc:
        raise ValueError(f"{name} must be an aware UTC datetime")


def _local_in_zone(value: datetime, name: str) -> tuple[datetime | None, str | None]:
    try:
        zone = ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None, "unknown_timezone"
    valid: set[datetime] = set()
    for fold in (0, 1):
        localized = value.replace(tzinfo=zone, fold=fold)
        instant = localized.astimezone(timezone.utc)
        back = instant.astimezone(zone)
        if back.replace(tzinfo=None) == value and back.fold == fold:
            valid.add(instant)
    if len(valid) == 1:
        return valid.pop(), None
    return (None, "ambiguous_local_time") if valid else (None, "nonexistent_local_time")


def _parse(raw: str, kind: str, suffix: str) -> tuple[datetime | None, tuple[str, ...]]:
    zone_match = _IANA.match(suffix)
    zone_name = zone_match.group(1) if zone_match else None
    if kind == "natural":
        return None, ("natural_language_date",)
    if kind == "date":
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None, ("invalid_date",)
        return None, ("time_missing", "timezone_missing")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, ("invalid_date",)
    if kind == "explicit":
        if abs(parsed.utcoffset()) > timedelta(hours=14):
            return None, ("invalid_timezone",)
        flags = []
        if zone_name:
            try:
                local = parsed.astimezone(ZoneInfo(zone_name))
            except ZoneInfoNotFoundError:
                flags.append("unknown_timezone")
            else:
                if (local.replace(tzinfo=None) != parsed.replace(tzinfo=None)
                        or local.utcoffset() != parsed.utcoffset()):
                    flags.append("timezone_conflict")
        return parsed.astimezone(timezone.utc), tuple(flags)
    if zone_name:
        instant, issue = _local_in_zone(parsed, zone_name)
        return instant, (issue,) if issue else ()
    return None, ("timezone_ambiguous",) if _VAGUE_ZONE.match(suffix) else ("timezone_missing",)


def _tokens(text: str):
    occupied = []
    tokens = []
    for kind, pattern in (("explicit", _EXPLICIT), ("local", _LOCAL),
                          ("date", _DATE), ("natural", _NATURAL)):
        for match in pattern.finditer(text):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            occupied.append(match.span())
            tokens.append((kind, match))
    yield from sorted(tokens, key=lambda item: item[1].start())


def extract_deadline_candidates(
    email: EmailRecord, triage: TriageResult, *, now: datetime,
) -> tuple[DeadlineCandidate, ...]:
    """Collect deadline-like dates from relevant mail without inferring missing facts."""
    _utc(now, "now")
    if triage.email_id != email.id:
        raise ValueError("triage result belongs to another email")
    if triage.career is False or triage.bucket in (TriageBucket.JUNK, TriageBucket.NEWSLETTER):
        return ()
    relevance_flags = []
    if triage.career is not True:
        relevance_flags.append("relevance_unverified")
    if triage.bucket is None:
        relevance_flags.append("triage_review")
    candidates = []
    subject_has_cue = _CUE.search(email.subject) is not None
    for field, text in (("subject", email.subject), ("body_excerpt", email.body_excerpt)):
        for kind, match in _tokens(text):
            prefix = text[max(0, match.start() - 100):match.start()]
            cue_matches = list(_CUE.finditer(prefix))
            if not cue_matches and not (field == "body_excerpt" and subject_has_cue):
                continue
            raw = match.group(0)
            deadline_at, flags = _parse(raw, kind, text[match.end():match.end() + 60])
            if kind == "explicit" and _VAGUE_ZONE_ANYWHERE.search(
                text[max(0, match.start() - 60):match.end() + 60]
            ) and not _IANA.match(text[match.end():match.end() + 60]):
                flags += ("timezone_label_unverified",)
            if not cue_matches:
                flags += ("subject_context_only",)
            if deadline_at is not None and deadline_at <= now:
                flags += ("past",)
            evidence_start = (
                max(0, match.start() - len(prefix) + cue_matches[-1].start())
                if cue_matches else max(0, match.start() - 30)
            )
            evidence_end = min(len(text), match.end() + 60)
            evidence = text[evidence_start:evidence_end].strip()
            candidates.append(DeadlineCandidate(
                stable_key("deadline-candidate", email.id, field, str(match.start()), raw),
                email.id, field, email.raw_link_reference or email.id,
                raw, evidence, deadline_at, tuple(dict.fromkeys((*flags, *relevance_flags))),
            ))
    if len(candidates) > 1:
        candidates = [replace(candidate, flags=(*candidate.flags, "multiple_candidates"))
                      for candidate in candidates]
    return tuple(candidates)


def validated_deadline_at(
    candidate: DeadlineCandidate, *, now: datetime,
    policy: DeadlineReviewPolicy | None = None,
) -> datetime:
    """Return a calendar-ready UTC instant or require an explicit review policy."""
    _utc(now, "now")
    if candidate.deadline_at is not None and not candidate.flags and candidate.deadline_at > now:
        return candidate.deadline_at
    if policy is None:
        raise DeadlineReviewRequired("deadline needs review before scheduling")
    if policy.candidate_id != candidate.id or policy.approved_at > now:
        raise DeadlineReviewRequired("review policy does not approve this candidate yet")
    if policy.confirmed_at <= now and not policy.allow_past:
        raise DeadlineReviewRequired("past deadline needs explicit past-date approval")
    return policy.confirmed_at


def validate_calendar_instant(
    deadline_at: datetime, *, now: datetime,
    policy: DeadlineReviewPolicy | None = None,
) -> datetime:
    """Guard the provider boundary against past instants from any caller."""
    if deadline_at.tzinfo is None or deadline_at.utcoffset() is None:
        raise ValueError("calendar deadline needs an aware instant")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now needs an aware instant")
    deadline_at = deadline_at.astimezone(timezone.utc)
    now = now.astimezone(timezone.utc)
    if deadline_at < now and (
        policy is None or not policy.allow_past or policy.confirmed_at != deadline_at
        or policy.approved_at > now
    ):
        raise DeadlineReviewRequired("past calendar deadline needs explicit review approval")
    return deadline_at
