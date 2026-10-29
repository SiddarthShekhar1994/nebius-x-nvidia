"""Command line entry point for Keeper's opportunity radar."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from keeper_core.adapters import AdapterError, AuthenticationError
from keeper_core.db import default_db_path
from keeper_core.radar import build_queries, load_profile
from keeper_core.radar_delivery import run_daily_radar
from keeper_core.time import to_storage


def _bounded(minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            number = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"must be an integer from {minimum} to {maximum}") from None
        if not minimum <= number <= maximum:
            raise argparse.ArgumentTypeError(f"must be between {minimum} and {maximum}")
        return number
    return parse


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="keeper radar")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="preview matches, or explicitly apply and deliver them")
    run.add_argument("--live", action="store_true", help="use Tavily and Nemotron Nano")
    run.add_argument("--apply", action="store_true", help="save to Keeper DB and deliver the digest")
    run.add_argument("--profile", type=Path, help="profile Markdown (defaults to the private live or synthetic demo profile)")
    run.add_argument("--db", type=Path, default=default_db_path())
    run.add_argument("--recipient", help="Telegram chat ID for --apply; defaults to TELEGRAM_CHAT_ID")
    run.add_argument("--secrets-dir", type=Path, default=Path("/sandbox/keeper/secrets"))
    run.add_argument("--run-id", help="daily run identity; defaults to the current UTC date")
    run.add_argument("--max-queries", type=_bounded(1, 12), default=6)
    run.add_argument("--results-per-query", type=_bounded(1, 20), default=5)

    review = commands.add_parser("review", help="preview or save a human-reviewed application page")
    review.add_argument("evidence", type=Path, help="reviewed evidence JSON file")
    review.add_argument("--review-id", required=True, help="new immutable review ID; reuse only for exact retries")
    review.add_argument("--reviewer", required=True)
    review.add_argument("--profile", type=Path, default=Path(os.environ.get("KEEPER_DATA_DIR", "data")) / "memory" / "profile.md")
    review.add_argument("--db", type=Path, default=default_db_path())
    review.add_argument("--apply", action="store_true", help="save the review locally; no provider calls")

    reviews = commands.add_parser("reviews", help="read saved source evidence and approval history")
    reviews.add_argument("--db", type=Path, default=default_db_path())
    reviews.add_argument("--opportunity", help="limit history to one opportunity ID")

    schedule = commands.add_parser("schedule", help="preview or approve one reviewed Calendar deadline")
    schedule.add_argument("review_id")
    schedule.add_argument("--db", type=Path, default=default_db_path())
    schedule.add_argument("--approve", action="store_true", help="explicitly approve this exact review revision")
    schedule.add_argument("--reviewer")
    schedule.add_argument("--reason")
    schedule.add_argument("--live", action="store_true")
    schedule.add_argument("--apply", action="store_true", help="record approval and synchronize Calendar")
    schedule.add_argument("--secrets-dir", type=Path, default=Path("/sandbox/keeper/secrets"))

    digest = commands.add_parser("digest", help="preview or deliver newly qualified reviewed candidates")
    digest.add_argument("--db", type=Path, default=default_db_path())
    digest.add_argument("--run-id", help="stable delivery identity for retries; defaults to the UTC date")
    digest.add_argument("--recipient", help="Telegram chat ID; defaults to TELEGRAM_CHAT_ID")
    digest.add_argument("--live", action="store_true")
    digest.add_argument("--apply", action="store_true")
    digest.add_argument("--secrets-dir", type=Path, default=Path("/sandbox/keeper/secrets"))
    return parser


def _profile_path(args: argparse.Namespace) -> Path:
    if args.profile is not None:
        return args.profile
    if args.live:
        return Path(os.environ.get("KEEPER_DATA_DIR", "data")) / "memory" / "profile.md"
    from demo.radar_lifecycle import PROFILE
    return PROFILE


def _offline_adapters(profile):
    from demo.radar_lifecycle import offline_adapters
    from keeper_core.fakes import FakeTelegram
    tavily, model = offline_adapters(profile)
    return tavily, model, FakeTelegram()


def _live_adapters(secrets_dir: Path):
    from integrations.radar_model import RadarRouterModel
    from integrations.tavily import LiveTavily
    from router.config import load_settings

    settings = load_settings()
    if not settings.api_key:
        raise AuthenticationError("Nebius key is missing")
    tavily = LiveTavily.from_key_file(secrets_dir / "tavily.key")
    return tavily, RadarRouterModel(settings)


def _live_telegram(secrets_dir: Path, db_path: Path):
    from integrations.telegram import LiveTelegram
    return LiveTelegram.from_token_file(secrets_dir / "telegram-bot.key", db_path)


def _live_calendar(secrets_dir: Path, db_path: Path):
    from integrations.calendar import LiveCalendar
    return LiveCalendar.from_token_file(secrets_dir / "google-token.json", db_path)


def _read_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError("Keeper database does not exist")
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)


def _copy_database(source_path: Path, destination_path: Path) -> None:
    if not source_path.exists():
        return
    if not source_path.is_file():
        raise ValueError("database path is not a file")
    source_uri = source_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=10)) as source, \
            closing(sqlite3.connect(destination_path, timeout=10)) as destination:
        source.backup(destination)


def _json_value(value):
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, datetime):
        return to_storage(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _report(result, *, mode: str, source: str, run_id: str) -> dict:
    digest = result.digest
    return {
        "mode": mode,
        "source": source,
        "scoring": "nano" if source == "live" else "simulated",
        "run_id": run_id,
        "queries": list(result.radar.results.queries),
        "opportunities": _json_value(result.radar.results.opportunities),
        "skipped": _json_value(result.radar.results.skipped),
        "digest": {
            "status": digest.status if mode == "apply" else ("preview" if digest.text else digest.status),
            "text": digest.text,
            "opportunity_ids": list(digest.opportunity_ids),
            "message_id": digest.message_id if mode == "apply" else None,
        },
    }


def _print_error(exc: Exception) -> None:
    if isinstance(exc, AdapterError):
        hint = "check provider credentials and network policy"
    elif isinstance(exc, sqlite3.Error):
        hint = "check the database path and permissions"
    elif isinstance(exc, OSError):
        hint = "check the profile and secret file paths"
    else:
        hint = "check the profile and radar configuration"
    print(f"keeper radar: failed ({type(exc).__name__}); {hint}.", file=sys.stderr)


def _run(args: argparse.Namespace) -> int:
    if args.apply and not args.live:
        print("keeper radar: --apply requires --live", file=sys.stderr)
        return 2
    recipient = args.recipient or os.environ.get("TELEGRAM_CHAT_ID", "")
    if args.apply and not recipient.strip():
        print("keeper radar: --apply requires --recipient or TELEGRAM_CHAT_ID", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    run_id = args.run_id if args.run_id is not None else f"radar:{now.date().isoformat()}"
    if not run_id.strip():
        print("keeper radar: --run-id must not be empty", file=sys.stderr)
        return 2

    try:
        profile = load_profile(_profile_path(args))
        queries = build_queries(profile, max_queries=args.max_queries)
        if not queries:
            raise ValueError("profile does not produce any radar queries")

        if args.live:
            tavily, model = _live_adapters(args.secrets_dir)
        else:
            tavily, model, preview_telegram = _offline_adapters(profile)

        if args.apply:
            telegram = _live_telegram(args.secrets_dir, args.db)
            result = run_daily_radar(
                args.db, profile, tavily, model, telegram,
                recipient_reference=recipient.strip(), run_id=run_id, now=now,
                max_queries=args.max_queries, results_per_query=args.results_per_query,
            )
            report = _report(result, mode="apply", source="live", run_id=run_id)
        else:
            with tempfile.TemporaryDirectory(prefix="keeper-radar-preview-") as directory:
                preview_db = Path(directory) / "keeper.db"
                if args.live:
                    _copy_database(args.db, preview_db)
                    from keeper_core.fakes import FakeTelegram
                    preview_telegram = FakeTelegram()
                result = run_daily_radar(
                    preview_db, profile, tavily, model, preview_telegram,
                    recipient_reference=(recipient.strip() or "preview") if args.live else "preview",
                    run_id=run_id, now=now,
                    max_queries=args.max_queries, results_per_query=args.results_per_query,
                )
                report = _report(result, mode="preview",
                                 source="live" if args.live else "synthetic", run_id=run_id)
        print(json.dumps(report, indent=2))
        return 0
    except Exception as exc:
        _print_error(exc)
        return 2


def _review(args: argparse.Namespace) -> int:
    from keeper_core.db import migrate
    from keeper_core.posting_reviews import load_posting_evidence, preview_posting_review, save_posting_review
    evidence = load_posting_evidence(args.evidence)
    profile = load_profile(args.profile)
    with closing(_read_connection(args.db)) as conn:
        result = preview_posting_review(conn, profile, evidence)
    if args.apply:
        migrate(args.db)
        with closing(sqlite3.connect(args.db, timeout=10)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN IMMEDIATE")
            report = save_posting_review(
                conn, profile, evidence, review_id=args.review_id,
                reviewed_by=args.reviewer, now=datetime.now(timezone.utc),
            )
    else:
        report = {"id": args.review_id, "opportunity_id": evidence.opportunity_id,
                  "source_url": evidence.source_url, "source_excerpt": evidence.source_excerpt,
                  "reviewed_by": args.reviewer, "eligibility": result.eligibility,
                  "eligibility_reason": result.eligibility_reason,
                  "deadline_at": evidence.deadline.deadline_at if evidence.deadline else None}
    print(json.dumps(_json_value({"mode": "applied" if args.apply else "preview", "review": report}), indent=2))
    return 0


def _schedule(args: argparse.Namespace) -> int:
    from keeper_core.posting_calendar import calendar_review_plan, schedule_reviewed_deadline
    if args.apply and not (
        args.live and args.approve and args.reviewer and args.reviewer.strip()
        and args.reason and args.reason.strip()
        and len(args.reviewer) <= 200 and len(args.reason) <= 1000
    ):
        print("keeper radar: scheduling requires --live --apply --approve --reviewer and --reason", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    with closing(_read_connection(args.db)) as conn:
        plan = calendar_review_plan(conn, args.review_id, now=now)
    if args.apply and plan["action"] != "review":
        calendar = _live_calendar(args.secrets_dir, args.db)
        result = schedule_reviewed_deadline(
            args.db, calendar, args.review_id,
            approved_by=args.reviewer, reason=args.reason, now=now,
        )
        report = {"mode": "apply", "calendar": result}
    else:
        report = {"mode": "preview", "calendar": plan}
    print(json.dumps(_json_value(report), indent=2))
    return 0


def _digest(args: argparse.Namespace) -> int:
    from keeper_core.db import migrate
    from keeper_core.fakes import FakeTelegram
    from keeper_core.radar_digest import send_radar_digest
    recipient = args.recipient or os.environ.get("TELEGRAM_CHAT_ID", "")
    if args.apply and not (args.live and recipient.strip()):
        print("keeper radar: delivery requires --live --apply and --recipient or TELEGRAM_CHAT_ID", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    run_id = args.run_id if args.run_id is not None else f"radar:{now.date().isoformat()}"
    if not run_id.strip():
        raise ValueError("run ID must not be empty")
    with closing(_read_connection(args.db)) as conn:
        conn.execute("SELECT id FROM opportunities LIMIT 1").fetchone()
    def deliver(path, telegram):
        migrate(path)
        with closing(sqlite3.connect(path, timeout=10)) as conn:
            return send_radar_digest(conn, telegram, run_id=run_id,
                                     recipient_reference=recipient.strip() or "preview", now=now)
    if args.apply:
        result = deliver(args.db, _live_telegram(args.secrets_dir, args.db))
    else:
        with tempfile.TemporaryDirectory(prefix="keeper-radar-digest-") as directory:
            temporary = Path(directory) / "keeper.db"
            _copy_database(args.db, temporary)
            result = deliver(temporary, FakeTelegram())
    report = {"mode": "apply" if args.apply else "preview", "run_id": run_id,
              "digest": {"status": result.status if args.apply or not result.text else "preview",
                         "text": result.text, "opportunity_ids": result.opportunity_ids,
                         "message_id": result.message_id if args.apply else None}}
    print(json.dumps(_json_value(report), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args)
    try:
        if args.command == "review":
            return _review(args)
        if args.command == "schedule":
            return _schedule(args)
        if args.command == "digest":
            return _digest(args)
        from keeper_core.posting_reviews import list_posting_reviews
        with closing(_read_connection(args.db)) as conn:
            print(json.dumps(_json_value(list_posting_reviews(conn, args.opportunity)), indent=2))
        return 0
    except Exception as exc:
        _print_error(exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
