"""Bounded opportunity discovery and conservative search-result screening."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Mapping
from urllib.parse import urlsplit

from .adapters import InvalidProviderResponse, ModelAdapter, SearchHit, TavilyAdapter
from .contracts import Eligibility, Opportunity
from .workflow import canonical_url, normalize_identity, opportunity_identity_key, opportunity_key, stable_key

if TYPE_CHECKING:
    from .radar_enrichment import VerifiedPostingRequirements
    from .radar_store import PersistedRadarRun


@dataclass(frozen=True, slots=True)
class RadarProfile:
    roles: tuple[str, ...]
    graduation_year: int | None
    majors: tuple[str, ...]
    locations: tuple[str, ...]
    location_unrestricted: bool
    remote: str | None
    relocation: str | None
    authorization_supplied: bool
    authorization_statement: str | None
    types: tuple[str, ...]
    topics: tuple[str, ...]
    exclusions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkippedHit:
    url: str
    reason: str


@dataclass(frozen=True, slots=True)
class RadarResults:
    queries: tuple[str, ...]
    opportunities: tuple[Opportunity, ...]
    skipped: tuple[SkippedHit, ...]


@dataclass(frozen=True, slots=True)
class RadarRunOutcome:
    results: RadarResults
    persisted: PersistedRadarRun


_FIELDS = {
    ("Target roles", "Roles"), ("Education", "Graduation year"), ("Education", "Major"),
    ("Location constraints", "Acceptable locations"), ("Location constraints", "Remote"),
    ("Location constraints", "Relocation"), ("Work authorization", "Voluntarily supplied"),
    ("Work authorization", "User statement"), ("Opportunity preferences", "Types"),
    ("Opportunity preferences", "Preferred topics"), ("Opportunity preferences", "Exclude"),
}
_AGGREGATORS = ("linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "simplyhired.com")
_PRIMARY_HOSTS = ("boards.greenhouse.io", "job-boards.greenhouse.io", "jobs.lever.co", "apply.workable.com", "jobs.ashbyhq.com", "myworkdayjobs.com", "devpost.com")
_ROLE_WORDS = re.compile(r"\b(intern|internship|engineer|developer|scientist|analyst|designer|researcher|hackathon|fellow)\b", re.I)
_CLOSED = re.compile(r"\b(no longer accepting applications|applications? (?:are |is )?closed|position (?:has been )?filled|posting expired|job expired)\b", re.I)
_SENIOR = re.compile(r"\b(senior|staff|principal|director|manager|lead)\b", re.I)
_CLAIM = re.compile(r"\b(citizenship|citizens?|security clearance|work authorization|visa sponsorship|sponsorship)\b", re.I)


def _list(value: str) -> tuple[str, ...]:
    return () if value.casefold() in ("not specified", "none", "any") else tuple(x.strip() for x in value.split(",") if x.strip())


def load_profile(path: Path) -> RadarProfile:
    """Read the documented Markdown labels; reject partial or contradictory profiles."""
    values: dict[tuple[str, str], str] = {}
    section = ""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        elif line.startswith("- ") and ":" in line:
            key, value = line[2:].split(":", 1)
            field = (section, key.strip())
            if field in _FIELDS:
                if field in values or not value.strip():
                    raise ValueError(f"duplicate or empty profile field: {section} / {key}")
                values[field] = value.strip()
    if set(values) != _FIELDS:
        raise ValueError(f"missing profile fields: {sorted(_FIELDS - set(values))}")
    get = lambda section, key: values[(section, key)]
    year = get("Education", "Graduation year")
    if year != "Not specified" and not re.fullmatch(r"\d{4}", year):
        raise ValueError("graduation year must be four digits or Not specified")
    supplied = get("Work authorization", "Voluntarily supplied")
    statement = get("Work authorization", "User statement")
    if supplied not in ("Yes", "No") or (supplied == "No" and statement != "Not specified") or (supplied == "Yes" and statement == "Not specified"):
        raise ValueError("work authorization statement must match voluntary-supply flag")
    remote = get("Location constraints", "Remote")
    relocation = get("Location constraints", "Relocation")
    if remote not in ("Allowed", "Required", "Not allowed", "Not specified") or relocation not in ("Allowed", "Not allowed", "Not specified"):
        raise ValueError("invalid location constraint")
    return RadarProfile(
        _list(get("Target roles", "Roles")), None if year == "Not specified" else int(year),
        _list(get("Education", "Major")), _list(get("Location constraints", "Acceptable locations")),
        get("Location constraints", "Acceptable locations") == "Any",
        None if remote == "Not specified" else remote,
        None if relocation == "Not specified" else relocation,
        supplied == "Yes", None if statement == "Not specified" else statement,
        _list(get("Opportunity preferences", "Types")), _list(get("Opportunity preferences", "Preferred topics")),
        _list(get("Opportunity preferences", "Exclude")),
    )


def _query_words(value: str, max_words: int = 6) -> str:
    # Strip search operators and punctuation from user-maintained data.
    return " ".join(re.findall(r"[\w]+", value, re.UNICODE)[:max_words])


def build_queries(profile: RadarProfile, *, max_queries: int = 6) -> tuple[str, ...]:
    if not 1 <= max_queries <= 12:
        raise ValueError("max_queries must be between 1 and 12")
    roles = profile.roles[:4] or profile.types[:2]
    types = profile.types[:2] or ("opportunity",)
    locations = profile.locations[:1]
    queries: list[str] = []
    def add_query(terms: tuple[str, ...]) -> bool:
        query = " ".join(term for term in terms if term)[:180].strip()
        if query and query not in queries:
            queries.append(query)
        return len(queries) >= max_queries

    for role in roles:
        for kind in types:
            terms = (_query_words(role), _query_words(kind), "careers apply",
                     _query_words(locations[0], 4) if locations else "")
            if add_query(terms):
                return tuple(queries)
    for role in roles:
        for topic in profile.topics[:2]:
            if add_query((_query_words(role), _query_words(topic), "careers apply",
                          _query_words(locations[0], 4) if locations else "")):
                return tuple(queries)
    return tuple(queries)


def _source_rank(url: str) -> int:
    host = (urlsplit(url).hostname or "").lower()
    if any(host == domain or host.endswith("." + domain) for domain in _AGGREGATORS):
        return 2
    if any(host == domain or host.endswith("." + domain) for domain in _PRIMARY_HOSTS):
        return 0
    if re.search(r"/(careers|jobs|positions|apply|internships|hackathons)(/|$)", urlsplit(url).path, re.I) or host.startswith(("careers.", "jobs.")):
        return 0
    if host.endswith((".edu", ".gov")):
        return 1
    return 3


def _title_and_organization(raw: str) -> tuple[str, str] | None:
    title = re.sub(r"\s+", " ", raw).strip()
    at = re.match(r"(.+?)\s+at\s+(.+)$", title, re.I)
    if at and _ROLE_WORDS.search(at[1]):
        return at[1].strip(), at[2].strip()
    for separator in (" | ", " — ", " - ", " · "):
        if separator in title:
            left, right = title.rsplit(separator, 1)
            if _ROLE_WORDS.search(left) and not _ROLE_WORDS.search(right):
                return left.strip(), right.strip()
            if _ROLE_WORDS.search(right) and not _ROLE_WORDS.search(left):
                return right.strip(), left.strip()
    return None


def _obvious_mismatch(profile: RadarProfile, title: str, snippet: str) -> str | None:
    text = f"{title} {snippet}"
    if _CLOSED.search(text):
        return "posting explicitly says applications are closed"
    if profile.exclusions and any(normalize_identity(term) in normalize_identity(text) for term in profile.exclusions if normalize_identity(term)):
        return "matches an explicit profile exclusion"
    student_search = any(re.search(r"intern|co.?op|new grad|hackathon", x, re.I) for x in profile.roles + profile.types)
    if student_search and _SENIOR.search(title):
        return "senior role conflicts with student opportunity preferences"
    title_words = set(normalize_identity(title).split())
    role_words = (
        set(normalize_identity(role).split()) - {"intern", "internship", "graduate", "new", "co", "op"}
        for role in profile.roles
    )
    type_only_match = any("hackathon" in normalize_identity(kind) and "hackathon" in title_words for kind in profile.types)
    if profile.roles and not any(words & title_words for words in role_words) and not type_only_match:
        return "title does not match target roles or opportunity types"
    return None


def normalize_hits(profile: RadarProfile, hits: tuple[SearchHit, ...], *, discovered_at: datetime) -> RadarResults:
    """Prefer primary URLs; retain one posting per URL and organization/title."""
    if discovered_at.tzinfo is not timezone.utc:
        raise ValueError("discovered_at must be UTC")
    accepted: list[Opportunity] = []
    skipped: list[SkippedHit] = []
    seen_urls: set[str] = set()
    seen_roles: set[str] = set()
    for hit in sorted(hits, key=lambda item: _source_rank(item.url)):
        try:
            url = canonical_url(hit.url)
        except ValueError:
            skipped.append(SkippedHit(hit.url, "invalid application URL"))
            continue
        rank = _source_rank(url)
        if rank == 2:
            skipped.append(SkippedHit(hit.url, "aggregator link; primary application URL not verified"))
            continue
        if rank == 3:
            skipped.append(SkippedHit(hit.url, "application source not verified"))
            continue
        parsed = _title_and_organization(hit.title)
        if not parsed:
            skipped.append(SkippedHit(hit.url, "title and organization cannot be verified from result"))
            continue
        title, organization = parsed
        mismatch = _obvious_mismatch(profile, title, hit.snippet)
        if mismatch:
            skipped.append(SkippedHit(hit.url, mismatch))
            continue
        identity = opportunity_identity_key(organization, title)
        if url in seen_urls or identity in seen_roles:
            skipped.append(SkippedHit(hit.url, "duplicate URL or organization/title in this search"))
            continue
        seen_urls.add(url)
        seen_roles.add(identity)
        reason = "Search snippet mentions eligibility; verify on the application page" if _CLAIM.search(hit.snippet) else "Eligibility requirements not verified on the application page"
        accepted.append(Opportunity(
            id=stable_key("radar-opportunity", url), dedupe_key=opportunity_key(url, organization, title),
            canonical_url=url, organization=organization, title=title,
            source=urlsplit(url).hostname or hit.source, discovered_at=discovered_at, score=None, explanation=None,
            eligibility=Eligibility.UNVERIFIED, eligibility_reason=reason,
            deadline=None, deadline_evidence=None,
        ))
    return RadarResults((), tuple(accepted), tuple(skipped))


def search_opportunities(profile: RadarProfile, tavily: TavilyAdapter, *, discovered_at: datetime,
                         max_queries: int = 6, results_per_query: int = 5) -> RadarResults:
    if not 1 <= results_per_query <= 20:
        raise ValueError("results_per_query must be between 1 and 20")
    queries = build_queries(profile, max_queries=max_queries)
    hits = tuple(hit for query in queries for hit in tavily.search(query, limit=results_per_query))
    result = normalize_hits(profile, hits, discovered_at=discovered_at)
    return RadarResults(queries, result.opportunities, result.skipped)


def run_radar(profile: RadarProfile, tavily: TavilyAdapter, model: ModelAdapter,
              conn: sqlite3.Connection, *, run_id: str, discovered_at: datetime,
              verified_requirements: Mapping[str, VerifiedPostingRequirements] | None = None,
              max_queries: int = 6, results_per_query: int = 5) -> RadarRunOutcome:
    """Search, score with Nano, check verified requirements, and save history.

    Caller owns the SQLite transaction and provides any application-page
    evidence. No requirement is inferred from a search snippet.
    """
    from .radar_enrichment import check_eligibility, score_opportunity
    from .radar_store import persist_radar_run

    if not 1 <= results_per_query <= 20:
        raise ValueError("results_per_query must be between 1 and 20")
    queries = build_queries(profile, max_queries=max_queries)
    hits = tuple(hit for query in queries for hit in tavily.search(query, limit=results_per_query))
    normalized = normalize_hits(profile, hits, discovered_at=discovered_at)
    by_url: dict[str, SearchHit] = {}
    for hit in hits:
        try:
            url = canonical_url(hit.url)
        except ValueError:
            continue
        if _source_rank(url) < 2:
            by_url.setdefault(url, hit)
    accepted: list[Opportunity] = []
    skipped = list(normalized.skipped)
    evidence = verified_requirements or {}
    for opportunity in normalized.opportunities:
        try:
            scored = score_opportunity(profile, opportunity, by_url[opportunity.canonical_url], model)
        except InvalidProviderResponse as exc:
            skipped.append(SkippedHit(opportunity.canonical_url, f"Nano relevance score unavailable: {exc}"))
            continue
        accepted.append(check_eligibility(profile, scored, evidence.get(opportunity.canonical_url)))
    persisted = persist_radar_run(conn, tuple(accepted), run_id=run_id)
    resolved = tuple(replace(item, id=keeper_id)
                     for item, keeper_id in zip(accepted, persisted.resolved_ids))
    return RadarRunOutcome(RadarResults(queries, resolved, tuple(skipped)), persisted)
