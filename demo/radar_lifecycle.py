"""Offline, repeatable synthetic opportunity radar scenario."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from keeper_core.adapters import ModelAnswer, SearchHit
from keeper_core.contracts import ModelMetadata
from keeper_core.fakes import FakeCalendar, FakeModel, FakeTavily, FakeTelegram
from keeper_core.radar import RadarProfile, build_queries, load_profile
from keeper_core.radar_delivery import run_daily_radar
from keeper_core.radar_store import opportunity_history


NOW = datetime(2026, 10, 28, 12, tzinfo=timezone.utc)
PROFILE = Path(__file__).with_name("profile.md")
MODEL_ID = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
HIT = SearchHit(
    "https://careers.example.test/jobs/software-engineer-intern",
    "Software Engineer Intern | Example Inc",
    "Join the AI infrastructure team building developer tools for production.",
    "careers.example.test",
)


def offline_adapters(profile: RadarProfile) -> tuple[FakeTavily, FakeModel]:
    """Build the deterministic local search and simulated scoring adapters."""
    query_results = {query: (HIT,) for query in build_queries(profile)}
    tavily = FakeTavily(query_results)
    model = FakeModel({
        "radar": ModelAnswer(
            json.dumps({
                "score": 8,
                "evidence": "Software Engineer Intern",
                "reason": "Direct match for a target software role",
            }),
            ModelMetadata(MODEL_ID, MODEL_ID, "offline-synthetic-radar-score-v1"),
        ),
    })
    return tavily, model


def run_scenario() -> dict:
    """Run discovery, same-run retry, and next-run dedupe using only local fakes."""
    profile = load_profile(PROFILE)
    tavily, model = offline_adapters(profile)
    telegram = FakeTelegram()
    calendar = FakeCalendar()

    with tempfile.TemporaryDirectory(prefix="keeper-radar-scenario-") as directory:
        db_path = Path(directory) / "keeper.db"
        first = run_daily_radar(
            db_path, profile, tavily, model, telegram,
            recipient_reference="scenario-chat", run_id="scenario:day-one", now=NOW,
            calendar=calendar,
        )
        retry = run_daily_radar(
            db_path, profile, tavily, model, telegram,
            recipient_reference="scenario-chat", run_id="scenario:day-one", now=NOW,
            calendar=calendar,
        )
        second_day = run_daily_radar(
            db_path, profile, tavily, model, telegram,
            recipient_reference="scenario-chat", run_id="scenario:day-two",
            now=NOW + timedelta(days=1), calendar=calendar,
        )

        if len(first.radar.results.opportunities) != 1:
            raise AssertionError("synthetic search should yield one candidate")
        candidate = first.radar.results.opportunities[0]
        with closing(sqlite3.connect(db_path)) as conn:
            opportunity_count = conn.execute("SELECT count(*) FROM opportunities").fetchone()[0]
            observation_count = conn.execute(
                "SELECT count(*) FROM opportunity_observations"
            ).fetchone()[0]
            history = opportunity_history(conn, candidate.id)

        if (opportunity_count, observation_count, len(history), len(telegram.messages_by_key),
                len(calendar.events_by_key)) != (1, 2, 2, 0, 0):
            raise AssertionError("scenario should deduplicate and hold unverified postings")
        if (first.digest.status, retry.digest.status, second_day.digest.status) != (
                "empty", "empty", "empty"):
            raise AssertionError("unverified eligibility must not produce a digest")

        return {
            "mode": "offline-synthetic",
            "candidate": {
                "title": candidate.title,
                "organization": candidate.organization,
                "url": candidate.canonical_url,
                "source": candidate.source,
                "snippet": HIT.snippet,
                "scoring": {
                    "mode": "simulated",
                    "requested_model": MODEL_ID,
                    "score": candidate.score,
                    "evidence": "Software Engineer Intern",
                    "reason": "Direct match for a target software role",
                },
                "eligibility": candidate.eligibility.value,
                "deadline": candidate.deadline,
            },
            "runs": [
                {"run_id": "scenario:day-one", "dedupe": "new_candidate",
                 "digest": first.digest.status},
                {"run_id": "scenario:day-one", "dedupe": "same_run_retry",
                 "digest": retry.digest.status},
                {"run_id": "scenario:day-two", "dedupe": "known_candidate_held_unverified",
                 "digest": second_day.digest.status},
            ],
            "counts": {
                "stored_opportunities": opportunity_count,
                "history_observations": observation_count,
                "telegram_messages": len(telegram.messages_by_key),
                "calendar_writes": len(calendar.events_by_key),
            },
        }


def main() -> int:
    print(json.dumps(run_scenario(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
