from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from family_safety_bot.config import RuleProfile

ActiveSession = tuple[int, datetime, int]


def _parse_stored_datetime(iso: str) -> datetime:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class ActivityClaim:
    claim_id: int
    child_id: str
    claimed_minutes: int
    description: str
    submitted_at: datetime


class PlaytimeStore:
    """Store playtime usage, sessions, and break tracking."""
    
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            self._ensure_schema(conn)

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        """Create tables for playtime tracking (multi-child)."""
        
        # Bank balance tracking per child
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bank (
                child_id TEXT PRIMARY KEY,
                balance_minutes INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        
        # Break balance tracking per child
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS break_balance (
                child_id TEXT PRIMARY KEY,
                balance_minutes INTEGER NOT NULL DEFAULT 0,
                last_update_iso TEXT NOT NULL
            );
            """
        )
        
        # Session tracking per child
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                child_id TEXT NOT NULL,
                start_time_iso TEXT NOT NULL,
                end_time_iso TEXT,
                minutes_granted INTEGER NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rule_profiles (
                profile_name TEXT PRIMARY KEY,
                weekly_addition_minutes INTEGER NOT NULL,
                max_bank_minutes INTEGER NOT NULL,
                break_balance_max_minutes INTEGER NOT NULL,
                break_recovery_rate REAL NOT NULL,
                blackout_periods_json TEXT NOT NULL
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS weekly_bank_baselines (
                child_id TEXT NOT NULL,
                week_start_iso TEXT NOT NULL,
                baseline_minutes INTEGER NOT NULL,
                PRIMARY KEY (child_id, week_start_iso)
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS activity_claims (
                claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
                child_id TEXT NOT NULL,
                claimed_minutes INTEGER NOT NULL,
                description TEXT NOT NULL,
                submitted_at_iso TEXT NOT NULL,
                handled INTEGER NOT NULL DEFAULT 0,
                granted_minutes INTEGER
            );
            """
        )

        conn.commit()

    @staticmethod
    def _serialize_blackout_periods(blackout_periods: list[tuple[int, str, str]]) -> str:
        serializable = [[weekday, start_time, end_time] for weekday, start_time, end_time in blackout_periods]
        return json.dumps(serializable, separators=(",", ":"))

    @staticmethod
    def _deserialize_blackout_periods(raw: str) -> list[tuple[int, str, str]]:
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(loaded, list):
            return []
        periods: list[tuple[int, str, str]] = []
        for item in loaded:
            if not isinstance(item, list) or len(item) != 3:
                continue
            weekday, start_time, end_time = item
            if not isinstance(weekday, int):
                continue
            periods.append((weekday, str(start_time), str(end_time)))
        return periods

    @classmethod
    def _rule_profile_from_row(cls, row: tuple) -> RuleProfile:
        return RuleProfile(
            name=row[0],
            weekly_addition_minutes=row[1],
            max_bank_minutes=row[2],
            break_balance_max_minutes=row[3],
            break_recovery_rate=row[4],
            blackout_periods=cls._deserialize_blackout_periods(row[5]),
        )

    def _set_app_state(self, key: str, value: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO app_state (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                """,
                (key, value),
            )
            conn.commit()

    def _get_app_state(self, key: str) -> str | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                """
                SELECT value
                FROM app_state
                WHERE key = ?;
                """,
                (key,),
            ).fetchone()
        return None if row is None else row[0]

    def _ensure_child_in_conn(self, conn: sqlite3.Connection, child_id: str) -> None:
        conn.execute(
            """
            INSERT OR IGNORE INTO bank (child_id, balance_minutes)
            VALUES (?, 0);
            """,
            (child_id,),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO break_balance (child_id, balance_minutes, last_update_iso)
            VALUES (?, 0, datetime('now'));
            """,
            (child_id,),
        )

    def get_bank_balance(self, child_id: str) -> int:
        """Get current bank balance in minutes for a child."""
        with sqlite3.connect(self._db_path) as conn:
            self._ensure_child_in_conn(conn, child_id)
            row = conn.execute(
                "SELECT balance_minutes FROM bank WHERE child_id = ?;",
                (child_id,),
            ).fetchone()
            assert row is not None
            return row[0]

    def set_bank_balance(self, child_id: str, balance_minutes: int) -> None:
        """Set bank balance for a child."""
        with sqlite3.connect(self._db_path) as conn:
            self._ensure_child_in_conn(conn, child_id)
            conn.execute(
                """
                UPDATE bank SET balance_minutes = ? WHERE child_id = ?;
                """,
                (balance_minutes, child_id),
            )
            conn.commit()

    def add_to_bank(self, child_id: str, minutes: int, max_balance: int) -> int:
        """Add minutes to bank, capping at max_balance. Returns actual amount added."""
        current = self.get_bank_balance(child_id)
        new_balance = min(current + minutes, max_balance)
        actual_added = new_balance - current
        self.set_bank_balance(child_id, new_balance)
        return actual_added

    def get_weekly_bank_baseline(self, child_id: str, week_start_iso: str, fallback_minutes: int) -> int:
        """Get baseline minutes for the given child/week, initializing from fallback if missing."""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                """
                SELECT baseline_minutes
                FROM weekly_bank_baselines
                WHERE child_id = ? AND week_start_iso = ?;
                """,
                (child_id, week_start_iso),
            ).fetchone()
            if row is not None:
                return row[0]

            conn.execute(
                """
                INSERT INTO weekly_bank_baselines (child_id, week_start_iso, baseline_minutes)
                VALUES (?, ?, ?);
                """,
                (child_id, week_start_iso, fallback_minutes),
            )
            conn.commit()
            return fallback_minutes

    def set_weekly_bank_baseline(self, child_id: str, week_start_iso: str, baseline_minutes: int) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO weekly_bank_baselines (child_id, week_start_iso, baseline_minutes)
                VALUES (?, ?, ?)
                ON CONFLICT(child_id, week_start_iso) DO UPDATE SET
                    baseline_minutes = excluded.baseline_minutes;
                """,
                (child_id, week_start_iso, max(0, baseline_minutes)),
            )
            conn.commit()

    def add_to_weekly_bank_baseline(self, child_id: str, week_start_iso: str, delta_minutes: int, fallback_minutes: int) -> int:
        """Increase baseline for the given week and return the new baseline."""
        baseline = self.get_weekly_bank_baseline(child_id, week_start_iso, fallback_minutes)
        new_baseline = max(0, baseline + delta_minutes)
        self.set_weekly_bank_baseline(child_id, week_start_iso, new_baseline)
        return new_baseline

    def get_consumed_break_debt(self, child_id: str) -> tuple[int, datetime]:
        """Get (consumed_break_debt_minutes, last_update_time) for a child."""
        with sqlite3.connect(self._db_path) as conn:
            self._ensure_child_in_conn(conn, child_id)
            row = conn.execute(
                "SELECT balance_minutes, last_update_iso FROM break_balance WHERE child_id = ?;",
                (child_id,),
            ).fetchone()
            assert row is not None
            return (row[0], _parse_stored_datetime(row[1]))

    def set_consumed_break_debt(self, child_id: str, debt_minutes: int, update_time: datetime) -> None:
        """Set consumed break debt and last update time for a child."""
        with sqlite3.connect(self._db_path) as conn:
            self._ensure_child_in_conn(conn, child_id)
            conn.execute(
                """
                UPDATE break_balance 
                SET balance_minutes = ?, last_update_iso = ? 
                WHERE child_id = ?;
                """,
                (max(0, debt_minutes), update_time.isoformat(), child_id),
            )
            conn.commit()

    def add_session(self, child_id: str, start_time: datetime, minutes_granted: int) -> int:
        """Record a new playtime session for a child. Returns session_id."""
        with sqlite3.connect(self._db_path) as conn:
            self._ensure_child_in_conn(conn, child_id)
            cursor = conn.execute(
                """
                INSERT INTO sessions (child_id, start_time_iso, minutes_granted)
                VALUES (?, ?, ?);
                """,
                (child_id, start_time.isoformat(), minutes_granted),
            )
            conn.commit()
            return cursor.lastrowid  # type: ignore

    def complete_session(self, session_id: int, end_time: datetime) -> None:
        """Mark a session as completed."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                UPDATE sessions
                SET end_time_iso = ?, completed = 1
                WHERE session_id = ?;
                """,
                (end_time.isoformat(), session_id),
            )
            conn.commit()

    def set_session_minutes_granted(self, session_id: int, minutes_granted: int) -> None:
        """Update granted minutes for an existing session."""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                UPDATE sessions
                SET minutes_granted = ?
                WHERE session_id = ?;
                """,
                (minutes_granted, session_id),
            )
            conn.commit()

    def get_active_session(self, child_id: str) -> ActiveSession | None:
        """Get the active session (session_id, start_time, minutes_granted) for a child if one exists."""
        with sqlite3.connect(self._db_path) as conn:
            self._ensure_child_in_conn(conn, child_id)
            row = conn.execute(
                """
                SELECT session_id, start_time_iso, minutes_granted
                FROM sessions
                WHERE child_id = ? AND completed = 0
                ORDER BY start_time_iso DESC
                LIMIT 1;
                """,
                (child_id,),
            ).fetchone()
            
            if row is None:
                return None
            
            return (row[0], datetime.fromisoformat(row[1]), row[2])

    def upsert_rule_profile(self, profile: RuleProfile) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """
                INSERT INTO rule_profiles (
                    profile_name,
                    weekly_addition_minutes,
                    max_bank_minutes,
                    break_balance_max_minutes,
                    break_recovery_rate,
                    blackout_periods_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_name) DO UPDATE SET
                    weekly_addition_minutes = excluded.weekly_addition_minutes,
                    max_bank_minutes = excluded.max_bank_minutes,
                    break_balance_max_minutes = excluded.break_balance_max_minutes,
                    break_recovery_rate = excluded.break_recovery_rate,
                    blackout_periods_json = excluded.blackout_periods_json;
                """,
                (
                    profile.name,
                    profile.weekly_addition_minutes,
                    profile.max_bank_minutes,
                    profile.break_balance_max_minutes,
                    profile.break_recovery_rate,
                    self._serialize_blackout_periods(profile.blackout_periods),
                ),
            )
            conn.commit()

    def list_rule_profiles(self) -> list[RuleProfile]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                """
                SELECT
                    profile_name,
                    weekly_addition_minutes,
                    max_bank_minutes,
                    break_balance_max_minutes,
                    break_recovery_rate,
                    blackout_periods_json
                FROM rule_profiles
                ORDER BY profile_name;
                """
            ).fetchall()
        return [self._rule_profile_from_row(row) for row in rows]

    def set_active_rule_profile_name_for_child(self, child_id: str, profile_name: str) -> None:
        self._set_app_state(f"active_rule_profile_name:{child_id}", profile_name)

    def get_active_rule_profile_name_for_child(self, child_id: str) -> str | None:
        return self._get_app_state(f"active_rule_profile_name:{child_id}")

    def set_child_block_mode(self, child_id: str, enabled: bool) -> None:
        self._set_app_state(f"grant_block_mode:{child_id}", "1" if enabled else "0")

    def is_child_block_mode_enabled(self, child_id: str) -> bool:
        return self._get_app_state(f"grant_block_mode:{child_id}") == "1"

    def add_activity_claim(
        self,
        child_id: str,
        claimed_minutes: int,
        description: str,
        submitted_at: datetime,
    ) -> int:
        """Store a new activity claim. Returns claim_id."""
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO activity_claims (child_id, claimed_minutes, description, submitted_at_iso)
                VALUES (?, ?, ?, ?);
                """,
                (child_id, claimed_minutes, description, submitted_at.isoformat()),
            )
            conn.commit()
            return cursor.lastrowid  # type: ignore

    def get_pending_claims(self, child_id: str | None = None) -> list[ActivityClaim]:
        """Return all unhandled claims, optionally filtered to one child."""
        with sqlite3.connect(self._db_path) as conn:
            if child_id is not None:
                rows = conn.execute(
                    """
                    SELECT claim_id, child_id, claimed_minutes, description, submitted_at_iso
                    FROM activity_claims
                    WHERE child_id = ? AND handled = 0
                    ORDER BY submitted_at_iso;
                    """,
                    (child_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT claim_id, child_id, claimed_minutes, description, submitted_at_iso
                    FROM activity_claims
                    WHERE handled = 0
                    ORDER BY submitted_at_iso;
                    """
                ).fetchall()
        return [
            ActivityClaim(
                claim_id=row[0],
                child_id=row[1],
                claimed_minutes=row[2],
                description=row[3],
                submitted_at=_parse_stored_datetime(row[4]),
            )
            for row in rows
        ]

    def mark_all_claims_handled(
        self,
        child_id: str,
        granted_minutes: int | None = None,
    ) -> int:
        """Mark all pending claims for a child as handled. Returns count handled."""
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(
                """
                UPDATE activity_claims
                SET handled = 1, granted_minutes = ?
                WHERE child_id = ? AND handled = 0;
                """,
                (granted_minutes, child_id),
            )
            conn.commit()
            return cursor.rowcount
