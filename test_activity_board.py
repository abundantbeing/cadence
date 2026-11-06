"""Behavior tests for activity-day streaks and model attribution."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

import activity_board
from activity_board import day, summary

_SCHEMA_SQL = """
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, source TEXT, model TEXT, billing_provider TEXT,
  parent_session_id TEXT, started_at REAL, title TEXT,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
  reasoning_tokens INTEGER DEFAULT 0
);
CREATE TABLE messages (
  session_id TEXT, role TEXT, timestamp REAL
);
CREATE TABLE session_model_usage (
  session_id TEXT, model TEXT, billing_provider TEXT,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
  reasoning_tokens INTEGER DEFAULT 0
);
"""


def _stamp(day: date, hour: int = 12, minute: int = 0) -> float:
    return datetime(day.year, day.month, day.day, hour, minute).timestamp()


def _insert_session(conn, sid: str, source: str, model: str, started: date, tokens: int, parent: str | None = None) -> None:
    conn.execute(
        """INSERT INTO sessions
           (id, source, model, billing_provider, parent_session_id, started_at, title, input_tokens, output_tokens)
           VALUES (?, ?, ?, 'test', ?, ?, ?, ?, 0)""",
        (sid, source, model, parent, _stamp(started), sid, tokens),
    )


def _insert_message(conn, sid: str, when: datetime) -> None:
    conn.execute("INSERT INTO messages VALUES (?, 'user', ?)", (sid, when.timestamp()))


class _Store:
    """One profile's state.db in a test."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(_SCHEMA_SQL)

    def _session(self, *args, **kwargs) -> None:
        _insert_session(self.conn, *args, **kwargs)

    def _message(self, sid: str, when: datetime) -> None:
        _insert_message(self.conn, sid, when)


class ActivityBoardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.db"
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(_SCHEMA_SQL)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _session(self, sid: str, source: str, model: str, started: date, tokens: int, parent: str | None = None) -> None:
        _insert_session(self.conn, sid, source, model, started, tokens, parent)

    def _message(self, sid: str, when: datetime) -> None:
        _insert_message(self.conn, sid, when)

    def test_resumed_chat_counts_the_later_day(self) -> None:
        today = date.today()
        yesterday = today - timedelta(days=1)
        self._session("chat", "desktop", "model-b", yesterday, 300)
        self._message("chat", datetime(today.year, today.month, today.day, 9, 0))
        self.conn.executemany(
            "INSERT INTO session_model_usage VALUES (?, ?, 'test', ?, 0, 0, 0, 0)",
            [("chat", "model-a", 100), ("chat", "model-b", 200)],
        )
        self.conn.commit()
        report = summary(path=self.path, today=today)
        active = {cell["day"] for cell in report["cells"] if cell["sessions"]}
        self.assertIn(today.isoformat(), active)
        self.assertNotIn(yesterday.isoformat(), active)
        self.assertEqual(report["streaks"]["current"], 1)
        models = {row["model"]: row["tokens"] for row in report["models"]}
        self.assertEqual(models, {"model-b": 200, "model-a": 100})

    def test_streak_holds_through_today_until_the_day_ends(self) -> None:
        today = date.today()
        self._session("chat", "desktop", "model-a", today - timedelta(days=2), 10)
        self._message("chat", datetime.combine(today - timedelta(days=2), datetime.min.time()).replace(hour=11))
        self._message("chat", datetime.combine(today - timedelta(days=1), datetime.min.time()).replace(hour=11))
        self.conn.commit()
        report = summary(path=self.path, today=today)
        self.assertEqual(report["streaks"]["current"], 2)
        self.assertEqual(report["streaks"]["longest"], 2)

    def test_range_week_and_rhythm_follow_message_time(self) -> None:
        today = date.today()
        old = today - timedelta(days=40)
        self._session("old", "cli", "old-model", old, 500)
        self._message("old", datetime(old.year, old.month, old.day, 9, 0))
        self._session("now", "desktop", "new-model", today, 100)
        when = datetime(today.year, today.month, today.day, 15, 0)
        self._message("now", when)
        self._message("now", when.replace(minute=10))
        self._message("now", when.replace(minute=20))
        self.conn.commit()
        report = summary(path=self.path, today=today)
        self.assertEqual(report["spans"]["30"]["tokens"], 100)
        self.assertEqual(report["spans"]["all"]["tokens"], 600)
        self.assertEqual(report["spans"]["30"]["models"][0]["model"], "new-model")
        names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        self.assertEqual(report["spans"]["all"]["rhythm"]["weekday"], names[today.weekday()])
        self.assertEqual(report["spans"]["all"]["rhythm"]["hour"], "3 PM")
        self.assertGreaterEqual(report["weeks"]["this"]["tokens"], 100)

    def test_cron_does_not_keep_a_streak(self) -> None:
        today = date.today()
        self._session("job", "cron", "model-a", today, 50)
        self._message("job", datetime(today.year, today.month, today.day, 8, 0))
        self.conn.commit()
        report = summary(path=self.path, today=today)
        self.assertEqual(report["streaks"]["current"], 0)
        self.assertEqual(report["totals"]["sessions"], 0)

    def test_longest_stretch_breaks_on_a_long_gap(self) -> None:
        today = date.today()
        start = datetime(today.year, today.month, today.day, 10, 0)
        self._session("chat", "desktop", "model-a", today, 20)
        self._message("chat", start)
        self._message("chat", start + timedelta(minutes=20))
        self._message("chat", start + timedelta(hours=3))
        self.conn.commit()
        report = summary(path=self.path, today=today)
        self.assertEqual(report["longest_stretch"]["label"], "20m")
        chosen = day(today.isoformat(), path=self.path)
        self.assertEqual(len(chosen["sessions"]), 1)

    def test_reasoning_tokens_are_not_counted_twice(self) -> None:
        # Hermes stores reasoning_tokens as the thinking part of output_tokens
        # (completion_tokens_details.reasoning_tokens), so a total adds output
        # once and never adds reasoning on top of it.
        today = date.today()
        self.conn.execute(
            """INSERT INTO sessions
               (id, source, model, billing_provider, started_at, title,
                input_tokens, output_tokens, reasoning_tokens)
               VALUES ('chat', 'desktop', 'model-a', 'test', ?, 'chat', 40, 100, 60),
                      ('solo', 'desktop', 'model-b', 'test', ?, 'solo', 0, 10, 4)""",
            (_stamp(today), _stamp(today)),
        )
        self.conn.execute(
            """INSERT INTO session_model_usage
               (session_id, model, billing_provider, input_tokens, output_tokens, reasoning_tokens)
               VALUES ('chat', 'model-a', 'test', 40, 100, 60)"""
        )
        self._message("chat", datetime(today.year, today.month, today.day, 9, 0))
        self._message("solo", datetime(today.year, today.month, today.day, 10, 0))
        self.conn.commit()
        report = summary(path=self.path, today=today)
        self.assertEqual(report["totals"]["tokens"], 150)
        self.assertEqual(report["spans"]["30"]["tokens"], 150)
        self.assertEqual({row["model"]: row["tokens"] for row in report["models"]}, {"model-a": 140, "model-b": 10})
        self.assertEqual({row["model"]: row["tokens"] for row in report["spans"]["30"]["models"]}, {"model-a": 140, "model-b": 10})
        chosen = day(today.isoformat(), path=self.path)
        self.assertEqual({row["id"]: row["tokens"] for row in chosen["sessions"]}, {"chat": 140, "solo": 10})

    def test_gap_day_breaks_the_longest_run(self) -> None:
        today = date.today()
        self._session("chat", "cli", "model-a", today - timedelta(days=4), 5)
        for offset in (4, 2, 1):
            when = today - timedelta(days=offset)
            self._message("chat", datetime(when.year, when.month, when.day, 12, 0))
        self.conn.commit()
        report = summary(path=self.path, today=today)
        self.assertEqual(report["streaks"]["longest"], 2)
        self.assertEqual(report["streaks"]["current"], 2)



class AllProfilesTests(unittest.TestCase):
    """``scope='all'`` merges every profile's store read-only."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stores: list[tuple[str, Path]] = []

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _store(self, name: str) -> _Store:
        store = _Store(self.root / f"{name}.db")
        self.stores.append((name, store.path))
        return store

    def test_profiles_merge_and_keep_their_own_ids(self) -> None:
        today = date.today()
        noon = datetime(today.year, today.month, today.day, 12, 0)
        for name, tokens in (("alpha", 100), ("beta", 300)):
            store = self._store(name)
            store._session("same-id", "desktop", "model-a", today, tokens)
            store._message("same-id", noon)
            store.conn.commit()
            store.conn.close()
        report = summary(scope="all", stores=self.stores, today=today)
        self.assertEqual(report["scope"], "all")
        self.assertEqual(report["included_profiles"], ["alpha", "beta"])
        self.assertEqual(report["totals"]["sessions"], 2)
        self.assertEqual(report["totals"]["tokens"], 400)
        self.assertEqual(report["streaks"]["current"], 1)
        self.assertEqual(
            [(row["profile"], row["sessions"], row["tokens"]) for row in report["profiles"]],
            [("beta", 1, 300), ("alpha", 1, 100)],
        )
        chosen = day(today.isoformat(), scope="all", stores=self.stores)
        self.assertEqual(sorted(row["profile"] for row in chosen["sessions"]), ["alpha", "beta"])
        self.assertEqual(len({row["id"] for row in chosen["sessions"]}), 2)

    def test_agent_only_days_do_not_keep_a_streak(self) -> None:
        today = date.today()
        store = self._store("worker")
        store._session("card", "kanban", "model-a", today, 70)
        store._message("card", datetime(today.year, today.month, today.day, 9, 0))
        store._session("child", "subagent", "model-a", today, 30)
        store._message("child", datetime(today.year, today.month, today.day, 9, 5))
        store.conn.commit()
        store.conn.close()
        report = summary(scope="all", stores=self.stores, today=today)
        self.assertEqual(report["streaks"]["current"], 0)
        self.assertEqual(report["weeks"]["this"]["days"], 1)
        self.assertEqual(report["totals"]["tokens"], 100)
        self.assertEqual(report["cells"][-7 + today.weekday()]["sessions"], 2)

    def test_unreadable_store_is_skipped(self) -> None:
        today = date.today()
        store = self._store("good")
        store._session("chat", "cli", "model-a", today, 5)
        store._message("chat", datetime(today.year, today.month, today.day, 9, 0))
        store.conn.commit()
        store.conn.close()
        broken = self.root / "broken.db"
        sqlite3.connect(broken).execute("CREATE TABLE unrelated (x)").connection.close()
        self.stores.append(("broken", broken))
        report = summary(scope="all", stores=self.stores, today=today)
        self.assertEqual(report["included_profiles"], ["good"])

    def test_profile_tokens_match_totals_without_messages(self) -> None:
        today = date.today()
        talking = self._store("talking")
        talking._session("chat", "cli", "model-a", today, 100)
        talking._message("chat", datetime(today.year, today.month, today.day, 9, 0))
        talking.conn.commit()
        talking.conn.close()
        quiet = self._store("quiet")
        quiet._session("chat", "cli", "model-a", today, 100)
        quiet.conn.commit()
        quiet.conn.close()
        report = summary(scope="all", stores=self.stores, today=today)
        self.assertEqual(report["totals"]["tokens"], 200)
        self.assertEqual(sum(row["tokens"] for row in report["profiles"]), 200)

    def test_empty_window_has_an_empty_agent_list(self) -> None:
        today = date.today()
        old = today - timedelta(days=100)
        store = self._store("old")
        store._session("chat", "cli", "model-a", old, 100)
        store._message("chat", datetime(old.year, old.month, old.day, 9, 0))
        store.conn.commit()
        store.conn.close()
        report = summary(scope="all", stores=self.stores, today=today)
        self.assertEqual(report["spans"]["30"]["profiles"], [])
        self.assertEqual(report["spans"]["all"]["profiles"][0]["tokens"], 100)

    def test_bad_timestamps_in_one_store_do_not_break_the_report(self) -> None:
        today = date.today()
        good = self._store("good")
        good._session("chat", "cli", "model-a", today, 5)
        good._message("chat", datetime(today.year, today.month, today.day, 9, 0))
        good.conn.commit()
        good.conn.close()
        odd = self._store("odd")
        odd.conn.execute("INSERT INTO sessions (id, source, started_at, input_tokens) VALUES ('x', 'cli', 'invalid', 7)")
        odd.conn.execute("INSERT INTO messages VALUES ('x', 'user', 'later')")
        odd.conn.commit()
        odd.conn.close()
        report = summary(scope="all", stores=self.stores, today=today)
        self.assertEqual(report["totals"]["tokens"], 12)
        self.assertEqual(report["streaks"]["current"], 1)

    def test_profile_discovery_honours_exclusions(self) -> None:
        (self.root / "state.db").touch()
        for name in ("alpha", "private", "no-config"):
            home = self.root / "profiles" / name
            home.mkdir(parents=True)
            (home / "state.db").touch()
            if name != "no-config":
                (home / "config.yaml").touch()
        names = [name for name, _ in activity_board.profile_stores(exclude={"private"}, root=self.root)]
        self.assertEqual(names, ["default", "alpha"])

    def test_scope_is_validated(self) -> None:
        with self.assertRaises(activity_board.ActivityError):
            summary(scope="everything", stores=self.stores)


if __name__ == "__main__":
    unittest.main()
