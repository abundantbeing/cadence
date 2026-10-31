"""Behavior tests for activity-day streaks and model attribution."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from activity_board import day, summary


def _stamp(day: date, hour: int = 12, minute: int = 0) -> float:
    return datetime(day.year, day.month, day.day, hour, minute).timestamp()


class ActivityBoardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.db"
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(
            """
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
        )

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def _session(self, sid: str, source: str, model: str, started: date, tokens: int, parent: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO sessions
               (id, source, model, billing_provider, parent_session_id, started_at, title, input_tokens, output_tokens)
               VALUES (?, ?, ?, 'test', ?, ?, ?, ?, 0)""",
            (sid, source, model, parent, _stamp(started), sid, tokens),
        )

    def _message(self, sid: str, when: datetime) -> None:
        self.conn.execute(
            "INSERT INTO messages VALUES (?, 'user', ?)", (sid, when.timestamp())
        )

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


if __name__ == "__main__":
    unittest.main()
