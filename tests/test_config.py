from __future__ import annotations

from family_safety_bot.config import Settings


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
    monkeypatch.setenv("MAX_BANK_TIME", "150m")
    monkeypatch.setenv("BREAK_BALANCE_MAX_TIME", "2h")
    _set_minimal_env(monkeypatch)

    settings = Settings.from_env()

    assert settings.default_rule_profile.weekly_addition_minutes == 90
    assert settings.default_rule_profile.max_bank_minutes == 150
    assert settings.default_rule_profile.break_balance_max_minutes == 120


def test_settings_time_accepts_compact_combined_notation(monkeypatch) -> None:
    monkeypatch.setenv("WEEKLY_ADDITION_TIME", "1h30m")
    monkeypatch.setenv("MAX_BANK_TIME", "42h")
    monkeypatch.setenv("BREAK_BALANCE_MAX_TIME", "3h")
    _set_minimal_env(monkeypatch)

    settings = Settings.from_env()

    assert settings.default_rule_profile.weekly_addition_minutes == 90


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
def test_settings_ignores_timezone_env_alias_and_uses_tz(monkeypatch) -> None:
    _set_minimal_env(monkeypatch)
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    monkeypatch.setenv("TZ", "Europe/Paris")

    settings = Settings.from_env()

    assert settings.timezone == "Europe/Paris"


def test_settings_defaults_timezone_when_only_timezone_alias_is_set(monkeypatch) -> None:
    _set_minimal_env(monkeypatch)
    monkeypatch.setenv("TIMEZONE", "America/New_York")

    settings = Settings.from_env()

    assert settings.timezone == "Europe/Helsinki"
