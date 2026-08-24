from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypeAlias

from family_safety_bot.config import BankProfile, RuleProfile

ActiveSession: TypeAlias = tuple[int, datetime, int]


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
    """Store playtime banks, sessions, profiles, and activity claims."""
    
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        with sqlite3.connect(self._db_path) as conn:
            self._ensure_schema(conn)

    @classmethod
    def _ensure_schema(cls, conn: sqlite3.Connection) -> None:
        """Create tables for playtime tracking (multi-child)."""
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bank (
                child_id TEXT NOT NULL,
                bank_name TEXT NOT NULL,
                balance_minutes INTEGER NOT NULL DEFAULT 0,
                last_update_iso TEXT NOT NULL,
                PRIMARY KEY (child_id, bank_name)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id INTEGER PRIMARY KEY AUTOINCREMENT,
                child_id TEXT NOT NULL,
                start_time_iso TEXT NOT NULL,
                end_time_iso TEXT,
                minutes_granted INTEGER NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS session_bank_debits (
                session_id INTEGER NOT NULL,
                bank_name TEXT NOT NULL,
                minutes_debited INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (session_id, bank_name)
            );
            CREATE TABLE IF NOT EXISTS play_day_taps (
                child_id TEXT NOT NULL,
                bank_name TEXT NOT NULL,
                local_date TEXT NOT NULL,
                session_id INTEGER NOT NULL,
                PRIMARY KEY (child_id, bank_name, local_date)
            );
            CREATE TABLE IF NOT EXISTS rule_profiles (
                profile_name TEXT PRIMARY KEY,
                banks_json TEXT NOT NULL,
                blackout_periods_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
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

    def _execute(
        self,
        sql: str,
        params: tuple = (),
        commit: bool = False,
    ) -> sqlite3.Cursor:
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(sql, params)
            if commit:
                conn.commit()
            return cursor

    @staticmethod
    def _serialize_blackout_periods(blackout_periods: list[tuple[int, str, str]]) -> str:
        serializable = [[weekday, start_time, end_time] for weekday, start_time, end_time in blackout_periods]
        return json.dumps(serializable, separators=(",", ":"))

    @staticmethod
    def _deserialize_blackout_periods(raw: str) -> list[tuple[int, str, str]]:
        return [
            (int(weekday), str(start_time), str(end_time))
            for weekday, start_time, end_time in json.loads(raw)
        ]

    @staticmethod
    def _serialize_banks(banks: dict[str, BankProfile]) -> str:
        return json.dumps(
            [
                {
                    "name": bank.name,
                    "weekly_addition_minutes": bank.weekly_addition_minutes,
                    "max_balance_minutes": bank.max_balance_minutes,
                    "recovery_rate": bank.recovery_rate,
                    "days": bank.days,
                }
                for bank in banks.values()
            ],
            separators=(",", ":"),
        )

    @staticmethod
    def _deserialize_banks(raw: str) -> dict[str, BankProfile]:
        loaded = json.loads(raw)
        return {
            str(item["name"]).lower(): BankProfile(
                name=str(item["name"]),
                weekly_addition_minutes=(
                    None if item["weekly_addition_minutes"] is None else int(item["weekly_addition_minutes"])
                ),
                max_balance_minutes=int(item["max_balance_minutes"]),
                recovery_rate=None if item["recovery_rate"] is None else float(item["recovery_rate"]),
                days=(
                    None
                    if item.get("days") is None
                    else tuple(int(day) for day in item["days"])
                ),
            )
            for item in loaded
        }

    @classmethod
    def _rule_profile_from_row(cls, row: tuple) -> RuleProfile:
        return RuleProfile(
            name=row[0],
            banks=cls._deserialize_banks(row[1]),
            blackout_periods=cls._deserialize_blackout_periods(row[2]),
        )

    def _set_app_state(self, key: str, value: str) -> None:
        self._execute(
            """
            INSERT INTO app_state (key, value)
            VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value;
            """,
            (key, value),
            commit=True,
        )

    def _get_app_state(self, key: str) -> str | None:
        row = self._execute(
            "SELECT value FROM app_state WHERE key = ?;",
            (key,),
        ).fetchone()
        return None if row is None else row[0]

    def get_bank_state(
        self,
        child_id: str,
        bank_name: str,
        initial_balance: int = 0,
        now: datetime | None = None,
    ) -> tuple[int, datetime]:
        """Return balance and update time, creating the bank when absent."""
        update_time = now or datetime.now(timezone.utc)
        self._execute(
            """INSERT OR IGNORE INTO bank (child_id, bank_name, balance_minutes, last_update_iso)
               VALUES (?, ?, ?, ?);""",
            (child_id, bank_name.lower(), initial_balance, update_time.isoformat()),
            commit=True,
        )
        row = self._execute(
            "SELECT balance_minutes, last_update_iso FROM bank WHERE child_id = ? AND bank_name = ?;",
            (child_id, bank_name.lower()),
        ).fetchone()
        assert row is not None
        return row[0], _parse_stored_datetime(row[1])

    def get_bank_balance(self, child_id: str, bank_name: str = "default") -> int:
        return self.get_bank_state(child_id, bank_name)[0]

    def set_bank_balance(
        self,
        child_id: str,
        balance_minutes: int,
        bank_name: str = "default",
        update_time: datetime | None = None,
    ) -> None:
        """Set one named bank balance for a child."""
        changed_at = update_time or datetime.now(timezone.utc)
        self.get_bank_state(child_id, bank_name, now=changed_at)
        self._execute(
            "UPDATE bank SET balance_minutes = ?, last_update_iso = ? WHERE child_id = ? AND bank_name = ?;",
            (balance_minutes, changed_at.isoformat(), child_id, bank_name.lower()),
            commit=True,
        )

    def add_to_bank(self, child_id: str, minutes: int, max_balance: int, bank_name: str = "default") -> int:
        """Add minutes to bank, capping at max_balance. Returns actual amount added."""
        current = self.get_bank_balance(child_id, bank_name)
        new_balance = min(current + minutes, max_balance)
        actual_added = new_balance - current
        self.set_bank_balance(child_id, new_balance, bank_name)
        return actual_added

    def add_session(self, child_id: str, start_time: datetime, minutes_granted: int) -> int:
        """Record a new playtime session for a child. Returns session_id."""
        cursor = self._execute(
            """
            INSERT INTO sessions (child_id, start_time_iso, minutes_granted)
            VALUES (?, ?, ?);
            """,
            (child_id, start_time.isoformat(), minutes_granted),
            commit=True,
        )
        return cursor.lastrowid  # type: ignore

    def complete_session(self, session_id: int, end_time: datetime) -> None:
        """Mark a session as completed."""
        self._execute(
            """
            UPDATE sessions
            SET end_time_iso = ?, completed = 1
            WHERE session_id = ?;
            """,
            (end_time.isoformat(), session_id),
            commit=True,
        )

    def set_session_minutes_granted(self, session_id: int, minutes_granted: int) -> None:
        """Update granted minutes for an existing session."""
        self._execute(
            """
            UPDATE sessions
            SET minutes_granted = ?
            WHERE session_id = ?;
            """,
            (minutes_granted, session_id),
            commit=True,
        )

    def add_session_bank_debit(self, session_id: int, bank_name: str, minutes: int) -> None:
        self._execute(
            """
            INSERT INTO session_bank_debits (session_id, bank_name, minutes_debited)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id, bank_name) DO UPDATE SET
                minutes_debited = minutes_debited + excluded.minutes_debited;
            """,
            (session_id, bank_name.lower(), minutes),
            commit=True,
        )

    def get_session_bank_debits(self, session_id: int) -> dict[str, int]:
        rows = self._execute(
            "SELECT bank_name, minutes_debited FROM session_bank_debits WHERE session_id = ?;",
            (session_id,),
        ).fetchall()
        return {row[0]: row[1] for row in rows}

    def has_play_day_tap(self, child_id: str, bank_name: str, local_date: str) -> bool:
        row = self._execute(
            """SELECT 1 FROM play_day_taps
               WHERE child_id = ? AND bank_name = ? AND local_date = ?;""",
            (child_id, bank_name.lower(), local_date),
        ).fetchone()
        return row is not None

    def tap_play_day(self, child_id: str, bank_name: str, local_date: str, session_id: int) -> bool:
        """Record and charge a bank's first play on a local date atomically."""
        with sqlite3.connect(self._db_path) as conn:
            existing = conn.execute(
                """SELECT 1 FROM play_day_taps
                   WHERE child_id = ? AND bank_name = ? AND local_date = ?;""",
                (child_id, bank_name.lower(), local_date),
            ).fetchone()
            if existing is not None:
                return False
            cursor = conn.execute(
                """UPDATE bank SET balance_minutes = balance_minutes - 1, last_update_iso = ?
                   WHERE child_id = ? AND bank_name = ? AND balance_minutes > 0;""",
                (datetime.now(timezone.utc).isoformat(), child_id, bank_name.lower()),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"No play days available in bank {bank_name!r}.")
            conn.execute(
                """INSERT INTO play_day_taps (child_id, bank_name, local_date, session_id)
                   VALUES (?, ?, ?, ?);""",
                (child_id, bank_name.lower(), local_date, session_id),
            )
            conn.commit()
        return True

    def get_active_session(self, child_id: str) -> ActiveSession | None:
        """Get the active session (session_id, start_time, minutes_granted) for a child if one exists."""
        row = self._execute(
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
        self._execute(
            """
            INSERT INTO rule_profiles (
                profile_name,
                banks_json,
                blackout_periods_json
            )
            VALUES (?, ?, ?)
            ON CONFLICT(profile_name) DO UPDATE SET
                banks_json = excluded.banks_json,
                blackout_periods_json = excluded.blackout_periods_json;
            """,
            (
                profile.name,
                self._serialize_banks(profile.banks),
                self._serialize_blackout_periods(profile.blackout_periods),
            ),
            commit=True,
        )

    def list_rule_profiles(self) -> list[RuleProfile]:
        rows = self._execute(
            """
            SELECT
                profile_name,
                banks_json,
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

    def set_recovery_interruption(
        self,
        child_id: str,
        recovery_started_by_bank: dict[str, datetime],
        interrupted_at: datetime,
    ) -> None:
        value = json.dumps(
            {
                "recovery_started": {
                    bank_name.lower(): started.isoformat()
                    for bank_name, started in recovery_started_by_bank.items()
                },
                "interrupted_at": interrupted_at.isoformat(),
            },
            separators=(",", ":"),
        )
        self._set_app_state(f"recovery_interruption:{child_id}", value)

    def get_recovery_interruption(self, child_id: str) -> tuple[dict[str, datetime], datetime] | None:
        raw = self._get_app_state(f"recovery_interruption:{child_id}")
        if not raw:
            return None
        loaded = json.loads(raw)
        starts = {
            str(bank_name): _parse_stored_datetime(started)
            for bank_name, started in loaded["recovery_started"].items()
        }
        return starts, _parse_stored_datetime(loaded["interrupted_at"])

    def clear_recovery_interruption(self, child_id: str) -> None:
        self._set_app_state(f"recovery_interruption:{child_id}", "")

    def add_activity_claim(
        self,
        child_id: str,
        claimed_minutes: int,
        description: str,
        submitted_at: datetime,
    ) -> int:
        """Store a new activity claim. Returns claim_id."""
        cursor = self._execute(
            """
            INSERT INTO activity_claims (child_id, claimed_minutes, description, submitted_at_iso)
            VALUES (?, ?, ?, ?);
            """,
            (child_id, claimed_minutes, description, submitted_at.isoformat()),
            commit=True,
        )
        return cursor.lastrowid  # type: ignore

    def get_pending_claims(self, child_id: str | None = None) -> list[ActivityClaim]:
        """Return all unhandled claims, optionally filtered to one child."""
        where_clause = "child_id = ? AND handled = 0" if child_id is not None else "handled = 0"
        params = (child_id,) if child_id is not None else ()
        rows = self._execute(
            f"""
            SELECT claim_id, child_id, claimed_minutes, description, submitted_at_iso
            FROM activity_claims
            WHERE {where_clause}
            ORDER BY submitted_at_iso;
            """,
            params,
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
        cursor = self._execute(
            """
            UPDATE activity_claims
            SET handled = 1, granted_minutes = ?
            WHERE child_id = ? AND handled = 0;
            """,
            (granted_minutes, child_id),
            commit=True,
        )
        return cursor.rowcount
