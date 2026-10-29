"""Command line for the router.

    py -m router smoke     one live call per task type, then the log rows
    py -m router recent    last 20 logged calls
    py -m router summary   totals per model
"""
from __future__ import annotations

import argparse
import sys

from . import calllog
from .config import load_settings
from .core import route
from .routes import TASK_TIERS

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}

SMOKE_PROMPTS = {
    "classify": "Classify this email subject as junk, actionable, due or newsletter. "
                "Reply with one word. Subject: 'Your weekly digest from Medium'",
    "triage": "Classify this email subject as junk, actionable, due or newsletter. "
              "Reply with one word. Subject: 'Hackathon registration closes Friday at noon'",
    "radar": "From 1 to 10, how well does this posting fit a computer science student looking "
             "for a software internship? Reply with the number only. "
             "Posting: 'Software Engineer Intern, Example Inc, summer 2027'",
    "tool_call": "What is the weather in Paris right now? Use the get_weather tool.",
    "draft": "Draft a two-sentence reply accepting a meeting on Thursday at 3pm.",
    "plan": "List three steps to prepare a hackathon demo video. Under 50 words.",
    "skill_write": "Write a short markdown skill with a name, a trigger and numbered steps "
                   "for archiving newsletters. Under 80 words.",
}


def _fmt_cost(cost):
    return "-" if cost is None else f"${cost:.6f}"


def cmd_smoke(settings) -> int:
    failures = 0
    for task_type in TASK_TIERS:
        tools = [WEATHER_TOOL] if task_type == "tool_call" else None
        messages = [{"role": "user", "content": SMOKE_PROMPTS[task_type]}]
        try:
            # The Nemotron 3 models reason before they answer; with a small cap a
            # one-word reply comes back empty, cut off mid-thought.
            resp = route(task_type, messages, tools, settings=settings, max_tokens=4096)
        except Exception as exc:
            failures += 1
            print(f"[FAIL] {task_type:<11} {type(exc).__name__}: {str(exc)[:150]}")
            continue
        msg = resp.choices[0].message
        asked = settings.models[TASK_TIERS[task_type]]
        if resp.model and resp.model.lower() != asked.lower():
            failures += 1
            print(f"[FAIL] {task_type:<11} asked for {asked} but {resp.model} answered")
            continue
        if task_type == "tool_call":
            calls = msg.tool_calls or []
            ok = bool(calls) and calls[0].function.name == "get_weather"
            detail = f"{calls[0].function.name}({calls[0].function.arguments})" if calls else "no tool_call"
        else:
            text = (msg.content or "").strip()
            ok = bool(text)
            detail = text[:70].replace("\n", " ") or \
                f"empty reply (finish_reason={resp.choices[0].finish_reason})"
        failures += not ok
        print(f"[{'OK  ' if ok else 'FAIL'}] {task_type:<11} {detail}")
    print()
    cmd_recent(settings, len(TASK_TIERS))
    return 1 if failures else 0


def cmd_recent(settings, limit: int = 20) -> int:
    rows = calllog.recent(settings.db_path, limit)
    print(f"Last {len(rows)} calls in {settings.db_path}")
    print(f"  {'time (UTC)':<19} {'task':<11} {'model':<40} {'tokens':>11} {'ms':>6} {'cost':>11}  status")
    for r in rows:
        tokens = "-" if r.prompt_tokens is None else f"{r.prompt_tokens}+{r.completion_tokens}"
        flag = r.status if not r.served_model or r.served_model.lower() == r.model.lower() \
            else f"{r.status} (served by {r.served_model})"
        print(f"  {r.ts[:19]:<19} {r.task_type:<11} {r.model:<40} {tokens:>11} "
              f"{r.latency_ms:>6} {_fmt_cost(r.cost_usd):>11}  {flag}")
    return 0


def cmd_summary(settings) -> int:
    rows = calllog.summary(settings.db_path)
    print(f"Totals in {settings.db_path}")
    print(f"  {'model':<40} {'calls':>5} {'errors':>6} {'in tok':>8} {'out tok':>8} {'avg ms':>7} {'cost':>11}")
    for model, calls, errors, p_tok, c_tok, avg_ms, cost in rows:
        print(f"  {model:<40} {calls:>5} {errors:>6} {p_tok:>8} {c_tok:>8} {avg_ms:>7} {_fmt_cost(cost):>11}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m router")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("smoke", help="one live call per task type")
    recent_p = sub.add_parser("recent", help="last logged calls")
    recent_p.add_argument("-n", type=int, default=20)
    sub.add_parser("summary", help="totals per model")
    args = parser.parse_args(argv)

    settings = load_settings()
    if args.command == "smoke":
        if not settings.api_key:
            print('TOKENFACTORY_API_KEY is not set. In PowerShell: $env:TOKENFACTORY_API_KEY = "..."')
            return 2
        return cmd_smoke(settings)
    if args.command == "recent":
        return cmd_recent(settings, args.n)
    return cmd_summary(settings)


if __name__ == "__main__":
    sys.exit(main())
