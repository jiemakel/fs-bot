from __future__ import annotations

from dataclasses import dataclass
from itertools import count
import os
from typing import Any

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
class BankProfile:
    """Balance limits and replenishment policy for one playtime bank."""

    name: str
    weekly_addition_minutes: int | None
    max_balance_minutes: int
    recovery_rate: float | None = None

    @property
    def is_recovery(self) -> bool:
        return self.recovery_rate is not None


@dataclass(frozen=True)
class RuleProfile:
    """Named playtime rules profile."""

    name: str
    banks: dict[str, BankProfile]
    blackout_periods: list[tuple[int, str, str]]

    @property
    def default_bank(self) -> BankProfile:
        return next(iter(self.banks.values()))


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


def parse_rule_profile_definition(name: str, definition: Any) -> RuleProfile:
    """Parse a strict JSON-compatible profile definition."""
    if not isinstance(definition, dict):
        raise ValueError("Profile definition must be a JSON object.")
    required_fields = {"banks", "blackouts"}
    if set(definition) != required_fields:
        raise ValueError(f"Profile fields must be exactly: {sorted(required_fields)}")

    raw_banks = definition["banks"]
    if not isinstance(raw_banks, dict) or not raw_banks:
        raise ValueError("Profile must define at least one bank.")
    banks: dict[str, BankProfile] = {}
    for raw_name, raw_bank in raw_banks.items():
        if not isinstance(raw_bank, dict):
            raise ValueError("Each bank must be a JSON object.")
        fields = set(raw_bank)
        weekly_fields = {"weekly_addition", "max_balance"}
        recovery_fields = {"recovery_rate", "max_balance"}
        if fields not in (weekly_fields, recovery_fields):
            raise ValueError("Each bank must define max_balance and exactly one replenishment policy.")
        bank_name = normalize_profile_name(str(raw_name))
        max_minutes = parse_duration_minutes(str(raw_bank["max_balance"]))
        if max_minutes is None or max_minutes <= 0:
            raise ValueError(f"Invalid maximum balance for bank {bank_name!r}.")
        weekly_minutes: int | None = None
        recovery_rate: float | None = None
        if fields == weekly_fields:
            weekly_minutes = parse_duration_minutes(str(raw_bank["weekly_addition"]))
            if weekly_minutes is None or weekly_minutes <= 0:
                raise ValueError(f"Invalid weekly addition for bank {bank_name!r}.")
        else:
            raw_rate = raw_bank["recovery_rate"]
            if isinstance(raw_rate, bool) or not isinstance(raw_rate, (int, float)) or raw_rate <= 0:
                raise ValueError(f"Invalid recovery rate for bank {bank_name!r}.")
            recovery_rate = float(raw_rate)
        key = bank_name.lower()
        if key in banks:
            raise ValueError(f"Duplicate bank name: {bank_name!r}.")
        banks[key] = BankProfile(bank_name, weekly_minutes, max_minutes, recovery_rate)

    raw_blackouts = definition["blackouts"]
    if not isinstance(raw_blackouts, list):
        raise ValueError("blackouts must be an array.")
    blackout_fields = {"days", "start", "end"}
    blackout_periods: list[tuple[int, str, str]] = []
    for raw_blackout in raw_blackouts:
        if not isinstance(raw_blackout, dict) or set(raw_blackout) != blackout_fields:
            raise ValueError(f"Blackout fields must be exactly: {sorted(blackout_fields)}")
        days = raw_blackout["days"]
        if not isinstance(days, list) or not days:
            raise ValueError("Each blackout must contain at least one day.")
        start = str(raw_blackout["start"])
        end = str(raw_blackout["end"])
        for day in days:
            day_key = str(day).lower()
            if day_key not in WEEKDAY_NAME_TO_INDEX:
                raise ValueError(f"Invalid blackout day: {day!r}.")
            blackout_periods.extend(
                parse_day_blackout_periods(
                    f"{start}-{end}",
                    WEEKDAY_NAME_TO_INDEX[day_key],
                    "profile JSON",
                )
            )

    return RuleProfile(
        name=normalize_profile_name(name),
        banks=banks,
        blackout_periods=blackout_periods,
    )


@dataclass(frozen=True)
class Settings:
    # Microsoft Family Safety credentials
    ms_family_email: str
    ms_family_password: str
    
    # Signal configuration
    children: dict[str, Child]  # Keyed by phone_number for fast lookup
    signal_admins: list[str]  # List of admin phone numbers
    signal_group_id: str  # Signal group ID where all communication happens

    # General settings
    timezone: str
    data_dir: str
    bot_language: str = "en"

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

        return Settings(
            ms_family_email=env.get("MS_FAMILY_EMAIL", ""),
            ms_family_password=env.get("MS_FAMILY_PASSWORD", ""),
            children=children,
            signal_admins=signal_admins,
            signal_group_id=signal_group_id,
            timezone=env.get("TZ", "Europe/Helsinki"),
            data_dir=env.get("DATA_DIR", "./data"),
            bot_language=env.get("BOT_LANGUAGE", "en"),
        )
