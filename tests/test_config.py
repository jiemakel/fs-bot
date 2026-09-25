from __future__ import annotations

import pytest

from family_safety_bot.config import Settings, parse_rule_profile_definition

_OPTIONAL_SETTINGS_ENV_KEYS = (
    "BOT_LANGUAGE",
    "TZ",
    "DATA_DIR",
    *[
        f"ADMIN_{index}_{suffix}"
        for index in range(1, 12)
        for suffix in ("PHONE", "MS_EMAIL")
    ],
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
    monkeypatch.setenv("SIGNAL_GROUP_ID", "group.test")
    monkeypatch.setenv("ADMIN_1_PHONE", "+10000000001")
    monkeypatch.setenv("ADMIN_1_MS_EMAIL", "parent@example.com")
    monkeypatch.setenv("CHILD_1_PHONE", "+10000000002")
    monkeypatch.setenv("CHILD_1_MS_ID", "child1")
    monkeypatch.setenv("CHILD_1_NAME", "Child1")


def test_parse_profile_definition_preserves_bank_order_and_shared_blackout_days() -> None:
    profile = parse_rule_profile_definition(
        "normal",
        {
            "banks": {
                "earned": {"weekly_addition": "1h30m", "max_balance": "42h"},
                "weekly": {"weekly_addition": "30h", "max_balance": "30h"},
                "recovery": {"recovery_rate": 3.0, "max_balance": "3h"},
            },
            "blackouts": [{"days": ["mon", "wed"], "start": "21:30", "end": "24:00"}],
        },
    )

    assert list(profile.banks) == ["earned", "weekly", "recovery"]
    assert profile.default_bank.name == "earned"
    assert profile.banks["earned"].weekly_addition_minutes == 90
    assert profile.banks["recovery"].recovery_rate == 3.0
    assert profile.blackout_periods == [(0, "21:30", "24:00"), (2, "21:30", "24:00")]


def test_parse_profile_definition_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="fields"):
        parse_rule_profile_definition(
            "invalid",
            {
                "banks": {"earned": {"weekly_addition": "14h", "max_balance": "42h"}},
                "blackouts": [],
                "typo": True,
            },
        )


def test_parse_profile_definition_requires_one_replenishment_policy_per_bank() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        parse_rule_profile_definition(
            "invalid",
            {
                "banks": {
                    "mixed": {
                        "weekly_addition": "1h",
                        "recovery_rate": 3.0,
                        "max_balance": "3h",
                    }
                },
                "blackouts": [],
            },
        )


def test_parse_profile_definition_accepts_day_banks() -> None:
    profile = parse_rule_profile_definition(
        "calendar",
        {
            "banks": {
                "weekdays": {
                    "days": ["mon", "tue", "wed", "thu", "fri"],
                    "weekly_addition": 3,
                    "max_balance": 5,
                },
                "weekends": {
                    "days": ["sat", "sun"],
                    "weekly_addition": "2d",
                    "max_balance": "4d",
                },
            },
            "blackouts": [],
        },
    )

    assert profile.banks["weekdays"].days == (0, 1, 2, 3, 4)
    assert profile.banks["weekdays"].weekly_addition_days == 3
    assert profile.banks["weekends"].max_balance_days == 4


def test_parse_profile_definition_accepts_and_normalizes_unicode_names() -> None:
    profile = parse_rule_profile_definition(
        "säännöt",
        {
            "banks": {
                "arkipa\u0308iva\u0308t": {
                    "days": ["mon", "tue", "wed", "thu", "fri"],
                    "weekly_addition": 3,
                    "max_balance": 4,
                }
            },
            "blackouts": [],
        },
    )

    assert profile.name == "säännöt"
    assert profile.banks["arkipäivät"].name == "arkipäivät"


def test_parse_profile_definition_rejects_invalid_day_bank_days() -> None:
    with pytest.raises(ValueError, match="Invalid day bank day"):
        parse_rule_profile_definition(
            "invalid",
            {
                "banks": {
                    "weekdays": {
                        "days": ["workday"],
                        "weekly_addition": 3,
                        "max_balance": 5,
                    }
                },
                "blackouts": [],
            },
        )


def test_settings_parses_bot_language(monkeypatch) -> None:
    _set_minimal_env(monkeypatch)
    monkeypatch.setenv("BOT_LANGUAGE", "fi")

    settings = Settings.from_env()

    assert settings.bot_language == "fi"


def test_settings_requires_an_admin_microsoft_email(monkeypatch) -> None:
    _set_minimal_env(monkeypatch)
    monkeypatch.delenv("ADMIN_1_MS_EMAIL")

    with pytest.raises(ValueError, match="ADMIN_n_MS_EMAIL"):
        Settings.from_env()


def test_settings_parses_more_than_nine_contiguous_admins_and_children(monkeypatch) -> None:
    monkeypatch.setenv("SIGNAL_GROUP_ID", "group.test")
    for index in range(1, 11):
        monkeypatch.setenv(f"ADMIN_{index}_PHONE", f"+100000000{index:02d}")
        monkeypatch.setenv(f"ADMIN_{index}_MS_EMAIL", f"organizer{index}@example.com")
        monkeypatch.setenv(f"CHILD_{index}_PHONE", f"+200000000{index:02d}")
        monkeypatch.setenv(f"CHILD_{index}_MS_ID", f"child{index}")
        monkeypatch.setenv(f"CHILD_{index}_NAME", f"Child{index}")

    settings = Settings.from_env()

    assert settings.signal_admins[-1] == "+10000000010"
    assert settings.admin_ms_emails["+10000000010"] == "organizer10@example.com"
    assert settings.children["+20000000010"].name == "Child10"
