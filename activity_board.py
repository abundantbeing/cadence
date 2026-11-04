"""Read-only Hermes activity: streaks, heatmap, model split, longest stretch.

Days come from message timestamps, not session creation. A chat started
yesterday and continued today counts today. Model totals come from
``session_model_usage``, so a mid-chat model switch is not collapsed onto
the session's last model. Cron and probe sources do not count as a
personal streak day.
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

PLUGIN_VERSION = "1.0.0"
HEATMAP_WEEKS = 53
STRETCH_GAP_SECONDS = 30 * 60
EXCLUDED_SOURCES = frozenset({
    "cron", "tool", "oneshot", "fallback-verification", "haiku55-sdk-health", "unknown",
})
TOKEN_COLS = (
    # reasoning_tokens is the thinking part of output_tokens, so it is not added again.
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
)


class ActivityError(Exception):
    """A problem the user can act on."""


def db_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "state.db"


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    target = path or db_path()
    if not target.exists():
        raise ActivityError("no session store yet (state.db missing)")
    conn = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _token_sql(cols: set[str], alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    parts = [f"COALESCE({prefix}{name}, 0)" for name in TOKEN_COLS if name in cols]
    return "+".join(parts) if parts else "0"


def _excluded_sql(alias: str) -> str:
    names = ", ".join("?" for _ in EXCLUDED_SOURCES)
    return f"COALESCE({alias}.source, '') NOT IN ({names})"


def _personal_where(session_alias: str = "s", parent_alias: str = "p") -> str:
    return (
        f"{_excluded_sql(session_alias)} AND {_excluded_sql(parent_alias)}"
    )


def _iso(day: date) -> str:
    return day.isoformat()


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ActivityError("day must be YYYY-MM-DD") from exc


def _streaks(active: set[str], today: date) -> dict[str, int]:
    longest = run = 0
    previous: Optional[date] = None
    for raw in sorted(active):
        current = _parse_day(raw)
        run = run + 1 if previous is not None and current == previous + timedelta(days=1) else 1
        longest = max(longest, run)
        previous = current
    end = today if _iso(today) in active else today - timedelta(days=1)
    current_run = 0
    cursor = end
    while _iso(cursor) in active:
        current_run += 1
        cursor -= timedelta(days=1)
    return {
        "current": current_run,
        "longest": max(longest, current_run),
        "active_days": len(active),
    }


def _duration_label(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{total}s"


def _heatmap_bounds(today: date) -> tuple[date, date]:
    monday = today - timedelta(days=today.weekday())
    start = monday - timedelta(weeks=HEATMAP_WEEKS - 1)
    end = monday + timedelta(days=6)
    return start, end


def _allocate(tokens: float, days: list[str]) -> dict[str, float]:
    if not days:
        return {}
    share = tokens / len(days)
    return {day: share for day in days}


def summary(*, path: Optional[Path] = None, today: Optional[date] = None) -> dict[str, Any]:
    today = today or date.today()
    start, grid_end = _heatmap_bounds(today)
    with closing(_connect(path)) as conn:
        session_cols = _columns(conn, "sessions")
        usage_cols = _columns(conn, "session_model_usage") if _table(conn, "session_model_usage") else set()
        message_cols = _columns(conn, "messages")
        if "timestamp" not in message_cols or "started_at" not in session_cols:
            raise ActivityError("session store is missing activity timestamps")
        tokens = _token_sql(session_cols, "s")
        params = tuple(EXCLUDED_SOURCES) + tuple(EXCLUDED_SOURCES)
        personal = _personal_where()
        day_rows = conn.execute(
            f"""SELECT date(m.timestamp, 'unixepoch', 'localtime') AS day,
                       s.id AS session_id,
                       COUNT(*) AS messages
                FROM messages m
                JOIN sessions s ON s.id = m.session_id
                LEFT JOIN sessions p ON p.id = s.parent_session_id
                WHERE m.timestamp IS NOT NULL AND {personal}
                GROUP BY day, s.id""",
            params,
        ).fetchall()
        session_rows = conn.execute(
            f"""SELECT s.id, s.title, s.source, s.model, s.billing_provider AS provider,
                       s.started_at, ({tokens}) AS tokens
                FROM sessions s
                LEFT JOIN sessions p ON p.id = s.parent_session_id
                WHERE {personal}""",
            params,
        ).fetchall()
        models = _models(conn, usage_cols, session_cols, params)
        longest = _longest_stretch(conn, params)
        model_rows = _model_rows(conn, usage_cols, params)
        rhythm_rows = conn.execute(
            f"""SELECT date(m.timestamp, 'unixepoch', 'localtime') AS day,
                       CAST(strftime('%w', m.timestamp, 'unixepoch', 'localtime') AS INTEGER) AS dow,
                       CAST(strftime('%H', m.timestamp, 'unixepoch', 'localtime') AS INTEGER) AS hour,
                       COUNT(*) AS messages
                FROM messages m
                JOIN sessions s ON s.id = m.session_id
                LEFT JOIN sessions p ON p.id = s.parent_session_id
                WHERE m.timestamp IS NOT NULL AND {personal}
                GROUP BY 1, 2, 3""",
            params,
        ).fetchall()

    by_session_days: dict[str, set[str]] = {}
    daily_sessions: dict[str, set[str]] = {}
    daily_messages: dict[str, int] = {}
    for row in day_rows:
        if not row["day"]:
            continue
        by_session_days.setdefault(row["session_id"], set()).add(row["day"])
        daily_sessions.setdefault(row["day"], set()).add(row["session_id"])
        daily_messages[row["day"]] = daily_messages.get(row["day"], 0) + int(row["messages"] or 0)

    sessions = {row["id"]: row for row in session_rows}
    daily_tokens: dict[str, float] = {}
    lifetime = 0.0
    for row in session_rows:
        amount = float(row["tokens"] or 0)
        lifetime += amount
        days = sorted(by_session_days.get(row["id"], set()))
        if not days and row["started_at"]:
            days = [date.fromtimestamp(row["started_at"]).isoformat()]
        for day, share in _allocate(amount, days).items():
            daily_tokens[day] = daily_tokens.get(day, 0) + share

    active = set(daily_sessions)
    streaks = _streaks(active, today)
    cells = []
    cursor = start
    while cursor <= grid_end:
        key = _iso(cursor)
        cells.append({
            "day": key,
            "weekday": cursor.weekday(),
            "future": cursor > today,
            "sessions": len(daily_sessions.get(key, ())),
            "messages": daily_messages.get(key, 0),
            "tokens": round(daily_tokens.get(key, 0)),
        })
        cursor += timedelta(days=1)
    peak = max(cells, key=lambda cell: cell["tokens"], default=None)
    sources: dict[str, int] = {}
    for row in session_rows:
        name = row["source"] or "unknown"
        sources[name] = sources.get(name, 0) + 1
    source_rows = [
        {"source": name, "sessions": count}
        for name, count in sorted(sources.items(), key=lambda item: item[1], reverse=True)[:8]
    ]
    return {
        "plugin_version": PLUGIN_VERSION,
        "generated_at": time.time(),
        "attribution": "estimated_by_active_days",
        "timezone": "local",
        "range": {"start": _iso(start), "end": _iso(min(grid_end, today))},
        "streaks": streaks,
        "totals": {
            "tokens": round(lifetime),
            "sessions": len(sessions),
            "peak_tokens": peak["tokens"] if peak else 0,
            "peak_day": peak["day"] if peak and peak["tokens"] else None,
        },
        "longest_stretch": longest,
        "models": models[:8],
        "sources": source_rows,
        "cells": cells,
        "spans": _spans(today, daily_tokens, daily_sessions, sessions, by_session_days, model_rows, rhythm_rows),
        "weeks": _week_compare(today, daily_tokens, daily_sessions),
    }


WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def _hour_label(hour: int) -> str:
    clock = hour % 12 or 12
    return f"{clock} {'AM' if hour < 12 else 'PM'}"


def _in_window(day_iso: str, today: date, days: Optional[int]) -> bool:
    chosen = date.fromisoformat(day_iso)
    if chosen > today:
        return False
    if days is None:
        return True
    return chosen >= today - timedelta(days=days - 1)


def _model_rows(conn: sqlite3.Connection, usage_cols: set[str], params: tuple) -> list[sqlite3.Row]:
    if not usage_cols:
        return []
    usage_tokens = _token_sql(usage_cols, "u")
    personal = _personal_where()
    return conn.execute(
        f"""SELECT u.session_id AS session_id,
                   COALESCE(NULLIF(u.model, ''), 'unknown') AS model,
                   COALESCE(u.billing_provider, '') AS provider,
                   SUM({usage_tokens}) AS tokens
            FROM session_model_usage u
            JOIN sessions s ON s.id = u.session_id
            LEFT JOIN sessions p ON p.id = s.parent_session_id
            WHERE {personal}
            GROUP BY u.session_id, 2, 3
            HAVING tokens > 0""",
        params,
    ).fetchall()


def _rhythm(rows: list[sqlite3.Row], today: date, days: Optional[int]) -> Optional[dict[str, Any]]:
    by_day: dict[int, int] = {}
    by_hour: dict[int, int] = {}
    for row in rows:
        if not row["day"] or not _in_window(row["day"], today, days):
            continue
        count = int(row["messages"] or 0)
        by_day[int(row["dow"])] = by_day.get(int(row["dow"]), 0) + count
        by_hour[int(row["hour"])] = by_hour.get(int(row["hour"]), 0) + count
    if not by_day or not by_hour:
        return None
    weekday = max(by_day, key=by_day.get)
    hour = max(by_hour, key=by_hour.get)
    return {
        "weekday": WEEKDAYS[weekday],
        "hour": _hour_label(hour),
        "messages": by_hour[hour],
    }


def _week_compare(today: date, daily_tokens: dict[str, float], daily_sessions: dict[str, set[str]]) -> dict[str, Any]:
    monday = today - timedelta(days=today.weekday())

    def pack(start: date, end: date) -> dict[str, Any]:
        tokens = 0.0
        active = 0
        cursor = start
        while cursor <= end:
            key = cursor.isoformat()
            tokens += daily_tokens.get(key, 0)
            if daily_sessions.get(key):
                active += 1
            cursor += timedelta(days=1)
        return {"tokens": round(tokens), "days": active, "start": start.isoformat(), "end": end.isoformat()}

    return {
        "this": pack(monday, today),
        "last": pack(monday - timedelta(days=7), monday - timedelta(days=1)),
    }


def _spans(
    today: date,
    daily_tokens: dict[str, float],
    daily_sessions: dict[str, set[str]],
    sessions: dict[str, sqlite3.Row],
    by_session_days: dict[str, set[str]],
    model_rows: list[sqlite3.Row],
    rhythm_rows: list[sqlite3.Row],
) -> dict[str, Any]:
    covered = {row["session_id"] for row in model_rows}
    model_days: dict[str, dict[tuple[str, Optional[str]], float]] = {}
    for row in model_rows:
        days = sorted(by_session_days.get(row["session_id"], set()))
        if not days:
            continue
        share = float(row["tokens"] or 0) / len(days)
        key = (row["model"], row["provider"] or None)
        for day_iso in days:
            model_days.setdefault(day_iso, {})
            model_days[day_iso][key] = model_days[day_iso].get(key, 0) + share
    for sid, row in sessions.items():
        if sid in covered:
            continue
        amount = float(row["tokens"] or 0)
        if amount <= 0:
            continue
        days = sorted(by_session_days.get(sid, set()))
        if not days and row["started_at"]:
            days = [date.fromtimestamp(row["started_at"]).isoformat()]
        if not days:
            continue
        share = amount / len(days)
        key = (row["model"] or "unknown", row["provider"] or None)
        for day_iso in days:
            model_days.setdefault(day_iso, {})
            model_days[day_iso][key] = model_days[day_iso].get(key, 0) + share

    def build(days: Optional[int]) -> dict[str, Any]:
        tokens = 0.0
        peak_day = None
        peak_tokens = 0.0
        seen: set[str] = set()
        sources: dict[str, int] = {}
        for day_iso, amount in daily_tokens.items():
            if not _in_window(day_iso, today, days):
                continue
            tokens += amount
            if amount > peak_tokens:
                peak_tokens = amount
                peak_day = day_iso
        for day_iso, ids in daily_sessions.items():
            if not _in_window(day_iso, today, days):
                continue
            for sid in ids:
                if sid in seen:
                    continue
                seen.add(sid)
                source = (sessions.get(sid)["source"] if sid in sessions else None) or "unknown"
                sources[source] = sources.get(source, 0) + 1
        model_totals: dict[tuple[str, Optional[str]], float] = {}
        for day_iso, bucket in model_days.items():
            if not _in_window(day_iso, today, days):
                continue
            for key, amount in bucket.items():
                model_totals[key] = model_totals.get(key, 0) + amount
        total = sum(model_totals.values()) or 1
        models = [
            {
                "model": key[0],
                "provider": key[1],
                "tokens": round(amount),
                "share": round(amount / total * 100),
            }
            for key, amount in sorted(model_totals.items(), key=lambda item: item[1], reverse=True)[:8]
            if amount > 0
        ]
        return {
            "tokens": round(tokens),
            "sessions": len(seen),
            "peak_tokens": round(peak_tokens),
            "peak_day": peak_day if peak_tokens else None,
            "models": models,
            "sources": [
                {"source": name, "sessions": count}
                for name, count in sorted(sources.items(), key=lambda item: item[1], reverse=True)[:8]
            ],
            "rhythm": _rhythm(rhythm_rows, today, days),
        }

    return {"30": build(30), "90": build(90), "all": build(None)}


def _table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _models(conn: sqlite3.Connection, usage_cols: set[str], session_cols: set[str], params: tuple) -> list[dict[str, Any]]:
    personal = _personal_where()
    rows: list[dict[str, Any]] = []
    covered: set[str] = set()
    if usage_cols:
        usage_tokens = _token_sql(usage_cols, "u")
        for row in conn.execute(
            f"""SELECT COALESCE(NULLIF(u.model, ''), 'unknown') AS model,
                       COALESCE(u.billing_provider, '') AS provider,
                       SUM({usage_tokens}) AS tokens,
                       COUNT(DISTINCT u.session_id) AS sessions
                FROM session_model_usage u
                JOIN sessions s ON s.id = u.session_id
                LEFT JOIN sessions p ON p.id = s.parent_session_id
                WHERE {personal}
                GROUP BY 1, 2
                HAVING tokens > 0
                ORDER BY tokens DESC""",
            params,
        ):
            covered.add(row["model"])
            rows.append({
                "model": row["model"],
                "provider": row["provider"] or None,
                "tokens": round(float(row["tokens"] or 0)),
                "sessions": int(row["sessions"] or 0),
            })
    if session_cols and usage_cols:
        tokens = _token_sql(session_cols, "s")
        for row in conn.execute(
            f"""SELECT s.id, COALESCE(NULLIF(s.model, ''), 'unknown') AS model,
                       s.billing_provider AS provider, ({tokens}) AS tokens
                FROM sessions s
                LEFT JOIN sessions p ON p.id = s.parent_session_id
                LEFT JOIN session_model_usage u ON u.session_id = s.id
                WHERE {personal} AND u.session_id IS NULL AND ({tokens}) > 0""",
            params,
        ):
            key = (row["model"], row["provider"] or None)
            found = next((item for item in rows if item["model"] == key[0] and item["provider"] == key[1]), None)
            if found is None:
                rows.append({"model": key[0], "provider": key[1], "tokens": 0, "sessions": 0})
                found = rows[-1]
            found["tokens"] += round(float(row["tokens"] or 0))
            found["sessions"] += 1
    total = sum(item["tokens"] for item in rows) or 1
    for item in rows:
        item["share"] = round(item["tokens"] / total * 100)
    return sorted(rows, key=lambda item: item["tokens"], reverse=True)


def _longest_stretch(conn: sqlite3.Connection, params: tuple) -> Optional[dict[str, Any]]:
    personal = _personal_where()
    best: Optional[dict[str, Any]] = None
    current_id = None
    title = source = None
    cluster_start = cluster_end = None
    previous = None

    def consider() -> None:
        nonlocal best
        if cluster_start is None or cluster_end is None:
            return
        seconds = cluster_end - cluster_start
        if seconds < 60:
            return
        if best is None or seconds > best["seconds"]:
            best = {
                "seconds": int(seconds),
                "label": _duration_label(seconds),
                "day": date.fromtimestamp(cluster_start).isoformat(),
                "session_id": current_id,
                "title": (title or "Untitled")[:80],
                "source": source or "unknown",
            }

    query = conn.execute(
        f"""SELECT s.id, s.title, s.source, m.timestamp
            FROM messages m
            JOIN sessions s ON s.id = m.session_id
            LEFT JOIN sessions p ON p.id = s.parent_session_id
            WHERE m.timestamp IS NOT NULL AND {personal}
            ORDER BY s.id, m.timestamp""",
        params,
    )
    for row in query:
        stamp = float(row["timestamp"])
        if row["id"] != current_id:
            consider()
            current_id = row["id"]
            title = row["title"]
            source = row["source"]
            cluster_start = cluster_end = previous = stamp
            continue
        if previous is not None and stamp - previous > STRETCH_GAP_SECONDS:
            consider()
            cluster_start = stamp
        cluster_end = stamp
        previous = stamp
    consider()
    return best


def day(day_iso: str, *, path: Optional[Path] = None) -> dict[str, Any]:
    chosen = _parse_day(day_iso)
    with closing(_connect(path)) as conn:
        session_cols = _columns(conn, "sessions")
        tokens = _token_sql(session_cols, "s")
        params = (day_iso, *EXCLUDED_SOURCES, *EXCLUDED_SOURCES)
        rows = conn.execute(
            f"""SELECT s.id, s.title, s.source, s.model, s.billing_provider AS provider,
                       MIN(m.timestamp) AS started_at, MAX(m.timestamp) AS ended_at,
                       COUNT(*) AS messages, ({tokens}) AS tokens
                FROM messages m
                JOIN sessions s ON s.id = m.session_id
                LEFT JOIN sessions p ON p.id = s.parent_session_id
                WHERE date(m.timestamp, 'unixepoch', 'localtime') = ?
                  AND {_personal_where()}
                GROUP BY s.id
                ORDER BY started_at""",
            params,
        ).fetchall()
    return {
        "plugin_version": PLUGIN_VERSION,
        "day": chosen.isoformat(),
        "sessions": [
            {
                "id": row["id"],
                "title": (row["title"] or "Untitled")[:80],
                "source": row["source"] or "unknown",
                "model": row["model"],
                "provider": row["provider"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "messages": int(row["messages"] or 0),
                "tokens": round(float(row["tokens"] or 0)),
            }
            for row in rows
        ],
    }
