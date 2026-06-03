from __future__ import annotations

from dataclasses import dataclass
import os

from family_safety_bot.durations import parse_duration_minutes

WEEKDAY_SUFFIXES = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
WEEKDAY_NAME_TO_INDEX = {
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}
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
    max_bank_minutes: int
    break_balance_max_minutes: int
    break_recovery_rate: float
    blackout_periods: list[tuple[int, str, str]]


def parse_clock_minutes(value: str) -> int | None:
    try:
        hours, minutes = map(int, value.split(":"))
    except ValueError:
        return None
    if hours == 24 and minutes == 0:
        return 24 * 60
    if 0 <= hours <= 23 and 0 <= minutes <= 59:
        return hours * 60 + minutes
    return None


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

    @staticmethod
    def _parse_duration_minutes(value: str, env_key: str) -> int:
        minutes = parse_duration_minutes(value)
        if minutes is None:
            raise ValueError(f"Invalid duration for {env_key}: {value!r}")
        return minutes

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
        children: dict[str, Child] = {}
        child_index = 1
        while phone := os.environ.get(f"CHILD_{child_index}_PHONE"):
            ms_id = os.environ.get(f"CHILD_{child_index}_MS_ID", "").strip()
            if not ms_id:
                raise ValueError(f"Missing required Microsoft child id: CHILD_{child_index}_MS_ID")
            name = os.environ.get(f"CHILD_{child_index}_NAME", "").strip()
            if not name:
                raise ValueError(f"Missing required child name: CHILD_{child_index}_NAME")
            children[phone] = Child(
                phone_number=phone,
                ms_account_id=ms_id,
                name=name,
            )
            child_index += 1
        return children

    @staticmethod
    def _parse_blackout_periods_from_env() -> list[tuple[int, str, str]]:
        blackout_periods: list[tuple[int, str, str]] = []
        for weekday, suffix in enumerate(WEEKDAY_SUFFIXES):
            day_periods = os.environ.get(f"BLACKOUT_PERIOD_{suffix}", "")
            if not day_periods:
                continue
            blackout_periods.extend(
                parse_day_blackout_periods(
                    day_periods,
                    weekday,
                    source=f"BLACKOUT_PERIOD_{suffix}",
                )
            )
        return blackout_periods

    @staticmethod
    def from_env() -> "Settings":
        ms_family_email = os.environ.get("MS_FAMILY_EMAIL", "")
        ms_family_password = os.environ.get("MS_FAMILY_PASSWORD", "")
        
        signal_group_id = os.environ.get("SIGNAL_GROUP_ID", "")
        
        signal_admins = Settings._parse_admins_from_env()
        children = Settings._parse_children_from_env()
        if not signal_group_id:
            raise ValueError("Missing required SIGNAL_GROUP_ID")
        if not signal_admins:
            raise ValueError("At least one ADMIN_n_PHONE is required")
        if not children:
            raise ValueError("At least one child is required")
        
        # Time parameters are duration strings externally, minute-based internally.
        weekly_addition_minutes = Settings._parse_duration_minutes(
            os.environ.get("WEEKLY_ADDITION_TIME", "14h"),
            "WEEKLY_ADDITION_TIME",
        )
        max_bank_minutes = Settings._parse_duration_minutes(
            os.environ.get("MAX_BANK_TIME", "42h"),
            "MAX_BANK_TIME",
        )
        # Note: break_balance_max_minutes also serves as the max per request.
        break_balance_max_minutes = Settings._parse_duration_minutes(
            os.environ.get("BREAK_BALANCE_MAX_TIME", "3h"),
            "BREAK_BALANCE_MAX_TIME",
        )
        break_recovery_rate = float(os.environ.get("BREAK_RECOVERY_RATE", "3.0"))
        
        blackout_periods = Settings._parse_blackout_periods_from_env()
        default_profile_name = normalize_profile_name(os.environ.get("RULE_PROFILE_DEFAULT_NAME", "default"))
        default_rule_profile = RuleProfile(
            name=default_profile_name,
            weekly_addition_minutes=weekly_addition_minutes,
            max_bank_minutes=max_bank_minutes,
            break_balance_max_minutes=break_balance_max_minutes,
            break_recovery_rate=break_recovery_rate,
            blackout_periods=blackout_periods,
        )
        
        timezone = os.environ.get("TZ", "Europe/Helsinki")
        data_dir = os.environ.get("DATA_DIR", "./data")

        return Settings(
            ms_family_email=ms_family_email,
            ms_family_password=ms_family_password,
            children=children,
            signal_admins=signal_admins,
            signal_group_id=signal_group_id,
            default_rule_profile=default_rule_profile,
            timezone=timezone,
            data_dir=data_dir,
        )
