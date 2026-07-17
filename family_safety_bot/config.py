from __future__ import annotations

from dataclasses import dataclass
from itertools import count
import os

from family_safety_bot.durations import parse_duration_minutes

WEEKDAY_SUFFIXES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
WEEKDAY_NAME_TO_INDEX = {suffix.lower(): i for i, suffix in enumerate(WEEKDAY_SUFFIXES)}
_PROFILE_NAME_ALLOWED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


@dataclass(frozen=True)
class Child:
    """Configuration for a single child."""
    phone_number: str  # Signal phone number (e.g., "+1234567890")
    ms_account_id: str  # Microsoft Family Safety account ID
    name: str  # Display name (e.g., "Alice")


@dataclass(frozen=True)
class RuleProfile:
    """Named playtime rules profile."""

    name: str
    weekly_addition_minutes: int
    weekly_max_minutes: int
    max_bank_minutes: int
    accrued_playtime_max_minutes: int
    break_recovery_rate: float
    blackout_periods: list[tuple[int, str, str]]


def parse_clock_minutes(value: str) -> int | None:
    try:
        hours, minutes = map(int, value.split(":"))
    except ValueError:
        return None
    if hours == 24 and minutes == 0:
        return 1440
    return hours * 60 + minutes if 0 <= hours <= 23 and 0 <= minutes <= 59 else None


def parse_day_blackout_periods(
    raw_periods: str,
    weekday: int,
    source: str,
) -> list[tuple[int, str, str]]:
    parsed: list[tuple[int, str, str]] = []
    for period in raw_periods.split(","):
        period = period.strip()
        if not period:
            continue
        if "-" not in period:
            raise ValueError(f"Invalid blackout period in {source}: {period!r}")
        start_time, end_time = period.split("-", 1)
        start_time = start_time.strip()
        end_time = end_time.strip()
        start_minutes = parse_clock_minutes(start_time)
        end_minutes = parse_clock_minutes(end_time)
        if start_minutes is None or end_minutes is None:
            raise ValueError(f"Invalid blackout period in {source}: {period!r}")
        if start_minutes >= end_minutes:
            raise ValueError(f"Blackout start must be before end in {source}: {period!r}")
        parsed.append((weekday, start_time, end_time))
    return parsed


def normalize_profile_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise ValueError("Profile name cannot be empty.")
    if not all(ch in _PROFILE_NAME_ALLOWED_CHARS for ch in normalized):
        raise ValueError("Profile name may only contain letters, numbers, '_' and '-'.")
    return normalized


@dataclass(frozen=True)
class Settings:
    # Microsoft Family Safety credentials
    ms_family_email: str
    ms_family_password: str
    
    # Signal configuration
    children: dict[str, Child]  # Keyed by phone_number for fast lookup
    signal_admins: list[str]  # List of admin phone numbers
    signal_group_id: str  # Signal group ID where all communication happens

    # Default profile from .env. Also used as template for newly created profiles.
    default_rule_profile: RuleProfile
    
    # General settings
    timezone: str
    data_dir: str
    bot_language: str = "en"

    @staticmethod
    def _parse_duration_minutes(value: str, env_key: str) -> int:
        if minutes := parse_duration_minutes(value):
            return minutes
        raise ValueError(f"Invalid duration for {env_key}: {value!r}")

    @staticmethod
    def _parse_admins_from_env() -> list[str]:
        admins: list[str] = []
        admin_index = 1
        while phone := os.environ.get(f"ADMIN_{admin_index}_PHONE"):
            admins.append(phone)
            admin_index += 1
        return admins

    @staticmethod
    def _parse_children_from_env() -> dict[str, Child]:
        children = {}
        for i in count(start=1):
            phone = os.environ.get(f"CHILD_{i}_PHONE")
            if phone is None:
                break
            ms_id = os.environ.get(f"CHILD_{i}_MS_ID", "").strip()
            name = os.environ.get(f"CHILD_{i}_NAME", "").strip()
            if not ms_id:
                raise ValueError(f"Missing required Microsoft child id: CHILD_{i}_MS_ID")
            if not name:
                raise ValueError(f"Missing required child name: CHILD_{i}_NAME")
            children[phone] = Child(phone, ms_id, name)
        return children

    @staticmethod
    def _parse_blackout_periods_from_env() -> list[tuple[int, str, str]]:
        return [
            period
            for weekday, suffix in enumerate(WEEKDAY_SUFFIXES)
            if (day_periods := os.environ.get(f"BLACKOUT_PERIOD_{suffix}", ""))
            for period in parse_day_blackout_periods(day_periods, weekday, f"BLACKOUT_PERIOD_{suffix}")
        ]

    @staticmethod
    def from_env() -> "Settings":
        env = os.environ
        children = Settings._parse_children_from_env()
        signal_admins = Settings._parse_admins_from_env()
        signal_group_id = env.get("SIGNAL_GROUP_ID", "")

        if not signal_group_id:
            raise ValueError("Missing required SIGNAL_GROUP_ID")
        if not signal_admins:
            raise ValueError("At least one ADMIN_n_PHONE is required")
        if not children:
            raise ValueError("At least one child is required")

        # Duration strings are external; RuleProfile stores minutes.
        return Settings(
            ms_family_email=env.get("MS_FAMILY_EMAIL", ""),
            ms_family_password=env.get("MS_FAMILY_PASSWORD", ""),
            children=children,
            signal_admins=signal_admins,
            signal_group_id=signal_group_id,
            default_rule_profile=RuleProfile(
                name=normalize_profile_name(env.get("RULE_PROFILE_DEFAULT_NAME", "default")),
                weekly_addition_minutes=Settings._parse_duration_minutes(env.get("WEEKLY_ADDITION_TIME", "14h"), "WEEKLY_ADDITION_TIME"),
                weekly_max_minutes=Settings._parse_duration_minutes(env.get("WEEKLY_MAX_TIME", "30h"), "WEEKLY_MAX_TIME"),
                max_bank_minutes=Settings._parse_duration_minutes(env.get("MAX_BANK_TIME", "42h"), "MAX_BANK_TIME"),
                # This also caps a single playtime request.
                accrued_playtime_max_minutes=Settings._parse_duration_minutes(env.get("ACCRUED_PLAYTIME_MAX_TIME", "3h"), "ACCRUED_PLAYTIME_MAX_TIME"),
                break_recovery_rate=float(env.get("BREAK_RECOVERY_RATE", "3.0")),
                blackout_periods=Settings._parse_blackout_periods_from_env(),
            ),
            timezone=env.get("TZ", "Europe/Helsinki"),
            data_dir=env.get("DATA_DIR", "./data"),
            bot_language=env.get("BOT_LANGUAGE", "en"),
        )
