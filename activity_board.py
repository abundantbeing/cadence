"""Read-only Hermes activity: streaks, heatmap, model split, longest stretch.

Days come from message timestamps, not session creation. A chat started
yesterday and continued today counts today. Model totals come from
``session_model_usage``, so a mid-chat model switch is not collapsed onto
the session's last model. Cron and probe sources do not count as a
personal streak day.

Scope is ``profile`` (the active profile's ``state.db``, the default) or
``all`` (every profile under the Hermes root, merged read-only in memory).
Set it with ``plugins.entries.cadence.settings.scope`` or ``--scope``;
``settings.exclude_profiles`` keeps named profiles out of the ``all`` view.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Mapping
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

PLUGIN_ID = "cadence"
PLUGIN_VERSION = "1.1.0"
HEATMAP_WEEKS = 53
STRETCH_GAP_SECONDS = 30 * 60
EXCLUDED_SOURCES = frozenset({
    "cron", "tool", "oneshot", "fallback-verification", "haiku55-sdk-health", "unknown",
})
# Work an agent ran on its own. It still shows in the heatmap and token totals,
# but a day with only this kind of session does not keep a personal streak.
STREAK_EXCLUDED_SOURCES = frozenset({"kanban", "subagent"})
TOKEN_COLS = (
    # reasoning_tokens is the thinking part of output_tokens, so it is not added again.
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
)
SCOPES = ("profile", "all")
_SESSION_FIELDS = (
    "id", "source", "model", "billing_provider", "parent_session_id", "started_at", "title", *TOKEN_COLS,
)
_USAGE_FIELDS = ("session_id", "model", "billing_provider", *TOKEN_COLS)

# Hermes stores Unix seconds; some bad rows use milliseconds or values datetime cannot represent.
_EPOCH_SECOND_MIN = 0.0
_EPOCH_SECOND_MAX = 4102444800.0  # 2100-01-01 UTC, generous upper bound for activity views


def _normalize_unix_seconds(raw: Optional[float]) -> Optional[float]:
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value != value or value in (float("inf"), float("-inf")):
        return None
    if value > _EPOCH_SECOND_MAX * 1000:
        return None
    if value > _EPOCH_SECOND_MAX:
        value = value / 1000.0
    if value < _EPOCH_SECOND_MIN or value > _EPOCH_SECOND_MAX:
        return None
    return value


def _day_iso_from_started(raw: Optional[float]) -> Optional[str]:
    seconds = _normalize_unix_seconds(raw if isinstance(raw, (int, float)) else None)
    if seconds is None:
        return None
    try:
        return date.fromtimestamp(seconds).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


class ActivityError(Exception):
    """A problem the user can act on."""


def db_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "state.db"


def _setting(key: str, default: Any = None) -> Any:
    """``plugins.entries.cadence.settings.<key>`` from the active profile's config."""
    try:
        from hermes_cli.config import load_config_readonly

        value: Any = load_config_readonly()
    except Exception:
        return default
    for segment in ("plugins", "entries", PLUGIN_ID, "settings", key):
        if not isinstance(value, Mapping) or segment not in value:
            return default
        value = value[segment]
    return value


def resolve_scope(scope: Optional[str] = None) -> str:
    chosen = scope or _setting("scope") or "profile"
    if chosen not in SCOPES:
        raise ActivityError("scope must be 'profile' or 'all'")
    return str(chosen)


def _excluded_profiles() -> set[str]:
    raw = _setting("exclude_profiles") or ()
    if isinstance(raw, str):
        raw = [raw]
    return {str(name) for name in raw if isinstance(name, str) or isinstance(name, int)}


def profile_stores(
    exclude: Optional[Iterable[str]] = None, *, root: Optional[Path] = None,
) -> list[tuple[str, Path]]:
    """``(profile name, state.db)`` for the default profile and every named profile."""
    if root is None:
        from hermes_constants import get_default_hermes_root

        root = Path(get_default_hermes_root())
    skip = _excluded_profiles() if exclude is None else set(exclude)
    found: list[tuple[str, Path]] = []
    if (root / "state.db").is_file():
        found.append(("default", root / "state.db"))
    profiles = root / "profiles"
    if profiles.is_dir():
        for entry in sorted(profiles.iterdir()):
            if entry.is_dir() and (entry / "config.yaml").is_file() and (entry / "state.db").is_file():
                found.append((entry.name, entry / "state.db"))
    return [(name, path) for name, path in found if name not in skip]


def _connect(path: Optional[Path] = None) -> sqlite3.Connection:
    target = path or db_path()
    if not target.exists():
        raise ActivityError("no session store yet (state.db missing)")
    conn = sqlite3.connect(f"{target.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _select_list(cols: set[str], fields: tuple[str, ...], alias: str) -> str:
    return ", ".join(f"{alias}.{name}" if name in cols else "NULL" for name in fields)


def _merged(stores: list[tuple[str, Path]]) -> tuple[sqlite3.Connection, list[str]]:
    """Copy the columns Cadence reads from each profile into one in-memory store.

    Each source is attached read-only, copied, and detached in turn, so SQLite's
    attached-database limit never applies. Session ids are prefixed with the
    profile name because two profiles can hold the same id.
    """
    if not stores:
        raise ActivityError("no profile session stores found")
    conn = sqlite3.connect(":memory:", uri=True)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        f"""CREATE TABLE sessions ({", ".join(_SESSION_FIELDS)}, profile TEXT);
            CREATE TABLE messages (session_id TEXT, timestamp REAL);
            CREATE TABLE session_model_usage ({", ".join(_USAGE_FIELDS)});"""
    )
    included: list[str] = []
    for name, path in stores:
        prefix = f"{name}/"
        try:
            conn.execute("ATTACH DATABASE ? AS src", (f"{Path(path).resolve().as_uri()}?mode=ro",))
        except sqlite3.Error:
            continue
        try:
            session_cols = {row[1] for row in conn.execute("PRAGMA src.table_info(sessions)")}
            message_cols = {row[1] for row in conn.execute("PRAGMA src.table_info(messages)")}
            if not {"id", "started_at"} <= session_cols or not {"session_id", "timestamp"} <= message_cols:
                continue
            has_parent = "parent_session_id" in session_cols
            # A non-numeric timestamp in one profile must not break the merged report.
            raw_started = "CASE WHEN typeof(s.started_at) IN ('integer', 'real') THEN s.started_at END"
            started_norm = (
                f"CASE WHEN ({raw_started}) IS NULL THEN NULL "
                f"WHEN ({raw_started}) > {_EPOCH_SECOND_MAX} AND ({raw_started}) <= {_EPOCH_SECOND_MAX} * 1000 "
                f"THEN ({raw_started}) / 1000.0 ELSE ({raw_started}) END"
            )
            started_ok = (
                f"({started_norm}) IS NOT NULL "
                f"AND ({started_norm}) >= {_EPOCH_SECOND_MIN} "
                f"AND ({started_norm}) <= {_EPOCH_SECOND_MAX}"
            )
            raw_ts = "CASE WHEN typeof(m.timestamp) IN ('integer', 'real') THEN m.timestamp END"
            ts_norm = (
                f"CASE WHEN ({raw_ts}) IS NULL THEN NULL "
                f"WHEN ({raw_ts}) > {_EPOCH_SECOND_MAX} AND ({raw_ts}) <= {_EPOCH_SECOND_MAX} * 1000 "
                f"THEN ({raw_ts}) / 1000.0 ELSE ({raw_ts}) END"
            )
            ts_ok = (
                f"({ts_norm}) IS NOT NULL "
                f"AND ({ts_norm}) >= {_EPOCH_SECOND_MIN} "
                f"AND ({ts_norm}) <= {_EPOCH_SECOND_MAX}"
            )
            has_usable_messages = f"EXISTS (SELECT 1 FROM src.messages m WHERE m.session_id = s.id AND {ts_ok})"
            token_parts = [
                f"ABS(COALESCE(s.{col}, 0))"
                for col in TOKEN_COLS
                if col in session_cols
            ]
            token_sum = "+".join(token_parts) if token_parts else "0"
            legacy_bad_start = (
                f"(typeof(s.started_at) NOT IN ('integer', 'real') AND ({token_sum}) > 0)"
            )
            keep_self = f"({has_usable_messages}) OR ({started_ok}) OR ({legacy_bad_start})"
            raw_started_c = "CASE WHEN typeof(c.started_at) IN ('integer', 'real') THEN c.started_at END"
            started_norm_c = (
                f"CASE WHEN ({raw_started_c}) IS NULL THEN NULL "
                f"WHEN ({raw_started_c}) > {_EPOCH_SECOND_MAX} AND ({raw_started_c}) <= {_EPOCH_SECOND_MAX} * 1000 "
                f"THEN ({raw_started_c}) / 1000.0 ELSE ({raw_started_c}) END"
            )
            started_ok_c = (
                f"({started_norm_c}) IS NOT NULL "
                f"AND ({started_norm_c}) >= {_EPOCH_SECOND_MIN} "
                f"AND ({started_norm_c}) <= {_EPOCH_SECOND_MAX}"
            )
            has_usable_messages_c = f"EXISTS (SELECT 1 FROM src.messages m WHERE m.session_id = c.id AND {ts_ok})"
            token_parts_c = [
                f"ABS(COALESCE(c.{col}, 0))"
                for col in TOKEN_COLS
                if col in session_cols
            ]
            token_sum_c = "+".join(token_parts_c) if token_parts_c else "0"
            legacy_bad_start_c = (
                f"(typeof(c.started_at) NOT IN ('integer', 'real') AND ({token_sum_c}) > 0)"
            )
            keep_child = f"({has_usable_messages_c}) OR ({started_ok_c}) OR ({legacy_bad_start_c})"
            keep_session = (
                f"({keep_self}) OR EXISTS ("
                f"SELECT 1 FROM src.sessions c WHERE c.parent_session_id = s.id AND ({keep_child})"
                f")"
            )
            parent = "CASE WHEN s.parent_session_id IS NULL THEN NULL ELSE ? || s.parent_session_id END"
            conn.execute(
                f"""INSERT INTO sessions
                    SELECT ? || s.id, {_select_list(session_cols, _SESSION_FIELDS[1:4], "s")},
                           {parent if has_parent else "NULL"},
                           CASE WHEN {started_ok} THEN {started_norm} ELSE NULL END,
                           {_select_list(session_cols, _SESSION_FIELDS[6:], "s")}, ?
                    FROM src.sessions s WHERE {keep_session}""",
                (prefix, prefix, name) if has_parent else (prefix, name),
            )
            conn.execute(
                f"""INSERT INTO messages SELECT ? || m.session_id, {ts_norm}
                    FROM src.messages m
                    JOIN src.sessions s ON s.id = m.session_id
                    WHERE {ts_ok} AND {keep_session}""",
                (prefix,),
            )
            usage_cols = {row[1] for row in conn.execute("PRAGMA src.table_info(session_model_usage)")}
            if "session_id" in usage_cols:
                rest = _select_list(usage_cols, _USAGE_FIELDS[1:], "u")
                conn.execute(
                    f"""INSERT INTO session_model_usage SELECT ? || u.session_id, {rest}
                        FROM src.session_model_usage u
                        JOIN src.sessions s ON s.id = u.session_id
                        WHERE {keep_session}""",
                    (prefix,),
                )
            conn.commit()
            included.append(name)
        except sqlite3.Error:
            conn.rollback()
        finally:
            conn.execute("DETACH DATABASE src")
    if not included:
        conn.close()
        raise ActivityError("no readable profile session stores found")
    conn.executescript(
        """CREATE INDEX messages_session ON messages(session_id, timestamp);
           CREATE INDEX sessions_id ON sessions(id);
           CREATE INDEX usage_session ON session_model_usage(session_id);"""
    )
    return conn, included


def _open(path: Optional[Path], scope: Optional[str], stores: Optional[list[tuple[str, Path]]]):
    """``(connection, profiles)``; *profiles* is ``None`` for a single-profile read."""
    if path is not None or resolve_scope(scope) == "profile":
        return _connect(path), None
    return _merged(profile_stores() if stores is None else stores)


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


def summary(
    *,
    path: Optional[Path] = None,
    today: Optional[date] = None,
    scope: Optional[str] = None,
    stores: Optional[list[tuple[str, Path]]] = None,
) -> dict[str, Any]:
    today = today or date.today()
    start, grid_end = _heatmap_bounds(today)
    conn, profiles = _open(path, scope, stores)
    profile_col = "s.profile" if profiles is not None else "NULL"
    with closing(conn):
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
                       s.started_at, ({tokens}) AS tokens, {profile_col} AS profile
                FROM sessions s
                LEFT JOIN sessions p ON p.id = s.parent_session_id
                WHERE {personal}""",
            params,
        ).fetchall()
        models = _models(conn, usage_cols, session_cols, params)
        longest = _longest_stretch(conn, params)
        model_rows = _model_rows(conn, usage_cols, params)
        # One local-time conversion per message: 'localtime' dominates the cost on a large store.
        rhythm_rows = [
            {"day": day_iso, "dow": int(dow), "hour": int(hour), "messages": row["messages"]}
            for row in conn.execute(
                f"""SELECT strftime('%Y-%m-%d|%w|%H', m.timestamp, 'unixepoch', 'localtime') AS bucket,
                           COUNT(*) AS messages
                    FROM messages m
                    JOIN sessions s ON s.id = m.session_id
                    LEFT JOIN sessions p ON p.id = s.parent_session_id
                    WHERE m.timestamp IS NOT NULL AND {personal}
                    GROUP BY 1""",
                params,
            )
            if row["bucket"]
            for day_iso, dow, hour in [row["bucket"].split("|")]
        ]

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
    session_shares: dict[str, dict[str, float]] = {}
    lifetime = 0.0
    for row in session_rows:
        amount = float(row["tokens"] or 0)
        lifetime += amount
        days = sorted(by_session_days.get(row["id"], set()))
        fallback = _day_iso_from_started(row["started_at"])
        if not days and fallback:
            days = [fallback]
        shares = _allocate(amount, days)
        session_shares[row["id"]] = shares
        for day, share in shares.items():
            daily_tokens[day] = daily_tokens.get(day, 0) + share

    active = {
        day_iso for day_iso, ids in daily_sessions.items()
        if any(_keeps_streak(sessions.get(sid)) for sid in ids)
    }
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
    spans = _spans(
        today, daily_tokens, daily_sessions, sessions, by_session_days, model_rows, rhythm_rows, session_shares,
        merged=profiles is not None,
    )
    return {
        "plugin_version": PLUGIN_VERSION,
        "generated_at": time.time(),
        "scope": "profile" if profiles is None else "all",
        "included_profiles": profiles,
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
        "profiles": spans["all"]["profiles"],
        "cells": cells,
        "spans": spans,
        "weeks": _week_compare(today, daily_tokens, daily_sessions),
    }


def _keeps_streak(row: Optional[sqlite3.Row]) -> bool:
    return row is not None and (row["source"] or "") not in STREAK_EXCLUDED_SOURCES


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


def _rhythm(rows: list[Mapping[str, Any]], today: date, days: Optional[int]) -> Optional[dict[str, Any]]:
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
        days = 0
        cursor = start
        while cursor <= end:
            key = cursor.isoformat()
            tokens += daily_tokens.get(key, 0)
            if daily_sessions.get(key):
                days += 1
            cursor += timedelta(days=1)
        return {"tokens": round(tokens), "days": days, "start": start.isoformat(), "end": end.isoformat()}

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
    rhythm_rows: list[Mapping[str, Any]],
    session_shares: Optional[dict[str, dict[str, float]]] = None,
    merged: bool = False,
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
        fallback = _day_iso_from_started(row["started_at"])
        if not days and fallback:
            days = [fallback]
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
        profiles: dict[str, dict[str, float]] = {}
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
                profile = sessions.get(sid)["profile"] if sid in sessions else None
                if profile:
                    profiles.setdefault(profile, {"sessions": 0, "tokens": 0.0})["sessions"] += 1
        # Tokens follow the same allocation as the totals, so a chat with no timed messages still counts.
        for sid, shares in (session_shares or {}).items():
            profile = sessions[sid]["profile"] if sid in sessions else None
            if not profile:
                continue
            amount = sum(value for day_iso, value in shares.items() if _in_window(day_iso, today, days))
            if amount:
                profiles.setdefault(profile, {"sessions": 0, "tokens": 0.0})["tokens"] += amount
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
            "profiles": [
                {"profile": name, "sessions": int(info["sessions"]), "tokens": round(info["tokens"])}
                for name, info in sorted(profiles.items(), key=lambda item: item[1]["tokens"], reverse=True)
            ] if merged else None,
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
            WHERE m.timestamp IS NOT NULL AND {personal}""",
        params,
    )
    rows = []
    for row in query:
        stamp = _normalize_unix_seconds(float(row["timestamp"]))
        if stamp is None:
            continue
        rows.append((row["id"], row["title"], row["source"], stamp))
    rows.sort(key=lambda item: (item[0], item[3]))
    for session_id, title_val, source_val, stamp in rows:
        row = {"id": session_id, "title": title_val, "source": source_val, "timestamp": stamp}
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


def day(
    day_iso: str,
    *,
    path: Optional[Path] = None,
    scope: Optional[str] = None,
    stores: Optional[list[tuple[str, Path]]] = None,
) -> dict[str, Any]:
    chosen = _parse_day(day_iso)
    conn, profiles = _open(path, scope, stores)
    profile_col = "s.profile" if profiles is not None else "NULL"
    with closing(conn):
        session_cols = _columns(conn, "sessions")
        tokens = _token_sql(session_cols, "s")
        params = (day_iso, *EXCLUDED_SOURCES, *EXCLUDED_SOURCES)
        rows = conn.execute(
            f"""SELECT s.id, s.title, s.source, s.model, s.billing_provider AS provider,
                       MIN(m.timestamp) AS started_at, MAX(m.timestamp) AS ended_at,
                       COUNT(*) AS messages, ({tokens}) AS tokens, {profile_col} AS profile
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
        "scope": "profile" if profiles is None else "all",
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
                "profile": row["profile"],
            }
            for row in rows
        ],
    }
