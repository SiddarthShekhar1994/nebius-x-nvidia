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
            "status": digest.status if mode == "apply" else ("preview" if digest.text else "empty"),
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
                    recipient_reference="preview", run_id=run_id, now=now,
                    max_queries=args.max_queries, results_per_query=args.results_per_query,
                )
                report = _report(result, mode="preview",
                                 source="live" if args.live else "synthetic", run_id=run_id)
        print(json.dumps(report, indent=2))
        return 0
    except Exception as exc:
        _print_error(exc)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
