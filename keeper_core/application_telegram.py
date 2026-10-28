"""Short plain-text, read-only application summary for Hermes /apps."""
from __future__ import annotations

import sqlite3
from collections import Counter

from .applications import list_applications


def _one_line(value: str, limit: int = 70) -> str:
    clean = " ".join(value.split())
    return clean[: limit - 1] + "…" if len(clean) > limit else clean


def format_apps_response(conn: sqlite3.Connection, *, max_chars: int = 3500) -> str:
    """Render a bounded Telegram-safe response from the product database."""
    if max_chars < 80:
        raise ValueError("max_chars must be at least 80")
    apps = list_applications(conn)
    if not apps:
        return "No applications tracked."
    counts = Counter(app.status.value for app in apps)
    lines = [
        f"Applications ({len(apps)})",
        " · ".join(f"{status.title()} {counts[status]}" for status in
                   ("found", "applied", "interview", "decision")),
    ]
    visible = 0
    for app in apps:
        organization, title = conn.execute(
            "SELECT organization, title FROM opportunities WHERE id = ?", (app.opportunity_id,),
        ).fetchone()
        state = app.status.value
        if app.decision_outcome:
            state += f" ({_one_line(app.decision_outcome, 30)})"
        line = f"• {_one_line(organization)} — {_one_line(title)} — {state}"
        remaining = len(apps) - visible - 1
        suffix = f"\n… and {remaining} more" if remaining else ""
        if len("\n".join((*lines, line))) + len(suffix) > max_chars:
            break
        lines.append(line)
        visible += 1
    if visible < len(apps):
        lines.append(f"… and {len(apps) - visible} more")
    return "\n".join(lines)
