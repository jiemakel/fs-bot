from __future__ import annotations

import pytest

from family_safety_bot.config import Settings, WEEKDAY_SUFFIXES

_OPTIONAL_SETTINGS_ENV_KEYS = (
    "BOT_LANGUAGE",
    "WEEKLY_ADDITION_TIME",
    "WEEKLY_MAX_TIME",
    "MAX_BANK_TIME",
    "ACCRUED_PLAYTIME_MAX_TIME",
    "BREAK_RECOVERY_RATE",
    "RULE_PROFILE_DEFAULT_NAME",
    "TZ",
    "DATA_DIR",
    *[f"BLACKOUT_PERIOD_{suffix}" for suffix in WEEKDAY_SUFFIXES],
    *[f"ADMIN_{index}_PHONE" for index in range(1, 12)],
    *[
        f"CHILD_{index}_{suffix}"
        for index in range(1, 12)
        for suffix in ("PHONE", "MS_ID", "NAME")
    ],
)


@pytest.fixture(autouse=True)
def _clear_settings_env(monkeypatch) -> None:
    for key in _OPTIONAL_SETTINGS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _set_minimal_env(monkeypatch) -> None:
    monkeypatch.setenv("MS_FAMILY_EMAIL", "parent@example.com")
    monkeypatch.setenv("MS_FAMILY_PASSWORD", "secret")
    monkeypatch.setenv("SIGNAL_GROUP_ID", "group.test")
    monkeypatch.setenv("ADMIN_1_PHONE", "+10000000001")
    monkeypatch.setenv("CHILD_1_PHONE", "+10000000002")
    monkeypatch.setenv("CHILD_1_MS_ID", "child1")
    monkeypatch.setenv("CHILD_1_NAME", "Child1")


def test_settings_time_accepts_h_m_and_h_plus_m(monkeypatch) -> None:
    monkeypatch.setenv("WEEKLY_ADDITION_TIME", "1h+30m")
    monkeypatch.setenv("WEEKLY_MAX_TIME", "30h")
    monkeypatch.setenv("MAX_BANK_TIME", "150m")
    monkeypatch.setenv("ACCRUED_PLAYTIME_MAX_TIME", "2h")
    _set_minimal_env(monkeypatch)

    settings = Settings.from_env()

    assert settings.default_rule_profile.weekly_addition_minutes == 90
    assert settings.default_rule_profile.weekly_max_minutes == 1800
    assert settings.default_rule_profile.max_bank_minutes == 150
    assert settings.default_rule_profile.accrued_playtime_max_minutes == 120


def test_settings_time_accepts_compact_combined_notation(monkeypatch) -> None:
    monkeypatch.setenv("WEEKLY_ADDITION_TIME", "1h30m")
    monkeypatch.setenv("MAX_BANK_TIME", "42h")
    monkeypatch.setenv("ACCRUED_PLAYTIME_MAX_TIME", "3h")
    _set_minimal_env(monkeypatch)

    settings = Settings.from_env()

    assert settings.default_rule_profile.weekly_addition_minutes == 90


def test_settings_weekly_max_defaults_to_30_hours(monkeypatch) -> None:
    _set_minimal_env(monkeypatch)

    settings = Settings.from_env()

    assert settings.default_rule_profile.weekly_max_minutes == 1800


def test_settings_parses_bot_language(monkeypatch) -> None:
    _set_minimal_env(monkeypatch)
    monkeypatch.setenv("BOT_LANGUAGE", "fi")

    settings = Settings.from_env()

    assert settings.bot_language == "fi"


def test_settings_parses_more_than_nine_contiguous_admins_and_children(monkeypatch) -> None:
    monkeypatch.setenv("MS_FAMILY_EMAIL", "parent@example.com")
    monkeypatch.setenv("MS_FAMILY_PASSWORD", "secret")
    monkeypatch.setenv("SIGNAL_GROUP_ID", "group.test")
    for index in range(1, 11):
        monkeypatch.setenv(f"ADMIN_{index}_PHONE", f"+100000000{index:02d}")
        monkeypatch.setenv(f"CHILD_{index}_PHONE", f"+200000000{index:02d}")
        monkeypatch.setenv(f"CHILD_{index}_MS_ID", f"child{index}")
        monkeypatch.setenv(f"CHILD_{index}_NAME", f"Child{index}")

    settings = Settings.from_env()

    assert settings.signal_admins[-1] == "+10000000010"
    assert settings.children["+20000000010"].name == "Child10"


def test_settings_parses_day_specific_blackout_periods_including_24_00(monkeypatch) -> None:
    _set_minimal_env(monkeypatch)
    monkeypatch.setenv("BLACKOUT_PERIOD_MON", "08:00-09:00,20:30-24:00")
    monkeypatch.setenv("BLACKOUT_PERIOD_WED", "14:15-15:45")

    settings = Settings.from_env()

    assert settings.default_rule_profile.blackout_periods == [
        (0, "08:00", "09:00"),
        (0, "20:30", "24:00"),
        (2, "14:15", "15:45"),
    ]
