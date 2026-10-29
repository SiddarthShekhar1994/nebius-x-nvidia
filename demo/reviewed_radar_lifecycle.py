"""Offline discovery, human review, approval, delivery, and same-event extension."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from demo.radar_lifecycle import NOW, PROFILE, offline_adapters
from keeper_core.deadlines import DeadlineReviewRequired
from keeper_core.fakes import FakeCalendar, FakeTelegram
from keeper_core.posting_calendar import calendar_review_plan, schedule_reviewed_deadline
from keeper_core.posting_reviews import parse_posting_evidence, save_posting_review
from keeper_core.radar import load_profile
from keeper_core.radar_delivery import run_daily_radar
from keeper_core.radar_digest import send_radar_digest


FIXTURE = Path(__file__).with_name("reviewed_posting.json")


def run_scenario() -> dict:
    profile = load_profile(PROFILE)
    tavily, model = offline_adapters(profile)
    telegram, calendar = FakeTelegram(), FakeCalendar()
    with tempfile.TemporaryDirectory(prefix="keeper-reviewed-radar-") as directory:
        db = Path(directory) / "keeper.db"
        discovery = run_daily_radar(
            db, profile, tavily, model, telegram,
            run_id="reviewed:discovery", recipient_reference="scenario-chat", now=NOW,
        )
        candidate = discovery.radar.results.opportunities[0]
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        payload["opportunity_id"] = candidate.id
        evidence = parse_posting_evidence(payload)
        with closing(sqlite3.connect(db)) as conn, conn:
            review = save_posting_review(conn, profile, evidence, review_id="page:first",
                                         reviewed_by="synthetic-reviewer", now=NOW)
        with closing(sqlite3.connect(db)) as conn:
            plan = calendar_review_plan(conn, "page:first", now=NOW)
            assert conn.execute("SELECT count(*) FROM deadline_events").fetchone()[0] == 0
            digest = send_radar_digest(conn, telegram, run_id="reviewed:qualified",
                                       recipient_reference="scenario-chat", now=NOW)
            replay = send_radar_digest(conn, telegram, run_id="reviewed:qualified",
                                       recipient_reference="scenario-chat", now=NOW)
        created = schedule_reviewed_deadline(
            db, calendar, "page:first", approved_by="synthetic-reviewer",
            reason="Reviewed the full synthetic posting and explicit deadline", now=NOW,
        )
        later = NOW + timedelta(days=1)
        payload["source_excerpt"] = payload["source_excerpt"].replace(
            "2026-12-10T17:00:00-08:00", "2026-12-15T17:00:00-08:00")
        payload["deadline_timestamp"] = "2026-12-15T17:00:00-08:00"
        with closing(sqlite3.connect(db)) as conn, conn:
            save_posting_review(conn, profile, parse_posting_evidence(payload),
                                review_id="page:extension", reviewed_by="synthetic-reviewer", now=later)
        try:
            schedule_reviewed_deadline(db, calendar, "page:first", approved_by="synthetic-reviewer",
                                       reason="Reviewed the full synthetic posting and explicit deadline", now=later)
        except DeadlineReviewRequired:
            stale = "rejected"
        else:
            raise AssertionError("the old review must not approve a changed posting")
        updated = schedule_reviewed_deadline(
            db, calendar, "page:extension", approved_by="synthetic-reviewer",
            reason="Reviewed the extended synthetic deadline", now=later,
        )
        unchanged = schedule_reviewed_deadline(
            db, calendar, "page:extension", approved_by="synthetic-reviewer",
            reason="Reviewed the extended synthetic deadline", now=later,
        )
        next_run = run_daily_radar(
            db, profile, tavily, model, telegram,
            run_id="reviewed:next-day", recipient_reference="scenario-chat", now=later,
        )
        with closing(sqlite3.connect(db)) as conn:
            counts = {"opportunities": conn.execute("SELECT count(*) FROM opportunities").fetchone()[0],
                      "observations": conn.execute("SELECT count(*) FROM opportunity_observations").fetchone()[0],
                      "reviews": conn.execute("SELECT count(*) FROM posting_reviews").fetchone()[0],
                      "approvals": conn.execute("SELECT count(*) FROM posting_reviews WHERE approved_at IS NOT NULL").fetchone()[0],
                      "calendar_events": len(calendar.events_by_key),
                      "telegram_messages": len(telegram.messages_by_key)}
        assert counts == {"opportunities": 1, "observations": 2, "reviews": 2,
                          "approvals": 2, "calendar_events": 1, "telegram_messages": 1}
        assert (discovery.digest.status, digest.status, next_run.digest.status) == ("empty", "sent", "empty")
        assert digest.message_id == replay.message_id
        assert created.provider_event_id == updated.provider_event_id == unchanged.provider_event_id
        assert (created.status, updated.status, unchanged.status) == ("created", "updated", "unchanged")
        return {"mode": "offline-synthetic", "scoring": "simulated", "opportunity_id": candidate.id,
                "discovery": {"eligibility": candidate.eligibility.value, "digest": discovery.digest.status},
                "review": {"eligibility": review["eligibility"].value, "preview_action": plan["action"]},
                "digest": {"after_review": digest.status, "retry": replay.status, "next_day": next_run.digest.status},
                "calendar": {"first_approval": created.status, "fresh_approval": updated.status,
                             "retry": unchanged.status, "stale_review": stale,
                             "provider_event_id": created.provider_event_id}, "counts": counts}


def main() -> int:
    print(json.dumps(run_scenario(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
