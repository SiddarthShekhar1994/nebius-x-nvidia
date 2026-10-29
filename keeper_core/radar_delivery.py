"""Daily radar orchestration with committed data before external side effects."""
from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping

from .adapters import CalendarAdapter, ModelAdapter, TavilyAdapter, TelegramAdapter
from .calendar_deadlines import CalendarSyncResult
from .contracts import Eligibility
from .db import migrate
from .posting_calendar import schedule_reviewed_deadline
from .posting_reviews import get_posting_review, parse_posting_evidence, reviewed_requirements
from .radar import RadarProfile, RadarRunOutcome, run_radar
from .radar_deadlines import VerifiedPostingDeadline
from .radar_digest import DigestResult, send_radar_digest
from .radar_enrichment import VerifiedPostingRequirements


@dataclass(frozen=True, slots=True)
class DailyRadarResult:
    radar: RadarRunOutcome
    digest: DigestResult
    deadlines: tuple[tuple[str, CalendarSyncResult], ...]


def run_daily_radar(db_path: Path, profile: RadarProfile, tavily: TavilyAdapter,
                    model: ModelAdapter, telegram: TelegramAdapter, *,
                    recipient_reference: str, run_id: str, now: datetime,
                    calendar: CalendarAdapter | None = None,
                    verified_requirements: Mapping[str, VerifiedPostingRequirements] | None = None,
                    verified_deadlines: Mapping[str, VerifiedPostingDeadline] | None = None,
                    max_queries: int = 6, results_per_query: int = 5) -> DailyRadarResult:
    """Persist search outcomes, send a frozen new-candidate digest, then schedule deadlines."""
    if verified_deadlines and calendar is None:
        raise ValueError("calendar adapter is required for verified deadlines")
    db_path = Path(db_path)
    migrate(db_path)
    with closing(sqlite3.connect(db_path, timeout=10)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        requirements = reviewed_requirements(conn)
        requirements.update(verified_requirements or {})
        radar = run_radar(
            profile, tavily, model, conn, run_id=run_id, discovered_at=now,
            verified_requirements=requirements,
            max_queries=max_queries, results_per_query=results_per_query,
        )
        conn.commit()
        digest = send_radar_digest(conn, telegram, run_id=run_id,
                                   recipient_reference=recipient_reference, now=now)
    scheduled: list[tuple[str, CalendarSyncResult]] = []
    if calendar is not None:
        for opportunity in radar.results.opportunities:
            deadline = (verified_deadlines or {}).get(opportunity.canonical_url)
            if deadline is not None:
                with closing(sqlite3.connect(db_path, timeout=10)) as conn:
                    latest = conn.execute(
                        """SELECT id FROM posting_reviews WHERE opportunity_id = ?
                           ORDER BY reviewed_at DESC, rowid DESC LIMIT 1""", (opportunity.id,),
                    ).fetchone()
                    review = get_posting_review(conn, latest[0]) if latest else None
                if opportunity.eligibility is not Eligibility.ELIGIBLE or not review or not review["approved_by"]:
                    result = CalendarSyncResult("review", None, None,
                                                "confirmed eligibility and saved deadline approval are required")
                else:
                    evidence = parse_posting_evidence({key: review[key] for key in (
                        "opportunity_id", "source_url", "source_excerpt", "requirements", "deadline_timestamp",
                    )})
                    if evidence.deadline != deadline:
                        result = CalendarSyncResult("review", None, None,
                                                    "deadline does not match the approved page revision")
                    else:
                        result = schedule_reviewed_deadline(
                            db_path, calendar, review["id"], approved_by=review["approved_by"],
                            reason=review["approval_reason"], now=now,
                        )
                scheduled.append((opportunity.id, result))
    return DailyRadarResult(radar, digest, tuple(scheduled))
