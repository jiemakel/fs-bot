from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from family_safety_bot.config import BankProfile, Child, RuleProfile, Settings
from family_safety_bot.storage import PlaytimeStore

CHILD_PHONE = "+1234567890"
ADMIN_PHONE = "+1234567891"
CHILD = Child(
    phone_number=CHILD_PHONE,
    ms_account_id="child123",
    name="TestChild",
)


def build_store(tmp_path: Path) -> PlaytimeStore:
    return PlaytimeStore(str(tmp_path / "test_playtime.db"))


def build_settings(tmp_path: Path, **overrides: Any) -> Settings:
    default_profile = RuleProfile(
        name="default",
        banks={"default": BankProfile("default", 840, 2520)},
        accrued_playtime_max_minutes=180,
        break_recovery_rate=3.0,
        blackout_periods=[],
    )
    values = {
        "ms_family_email": "test@example.com",
        "ms_family_password": "password",
        "children": {CHILD_PHONE: CHILD},
        "signal_admins": [ADMIN_PHONE],
        "signal_group_id": "test_group",
        "default_rule_profile": default_profile,
        "timezone": "UTC",
        "data_dir": str(tmp_path),
    }
    profile_updates = {
        key: overrides.pop(key)
        for key in (
            "banks",
            "accrued_playtime_max_minutes",
            "break_recovery_rate",
            "blackout_periods",
        )
        if key in overrides
    }
    if profile_updates:
        default_profile = replace(default_profile, **profile_updates)
    values["default_rule_profile"] = default_profile
    values.update(overrides)
    return Settings(**values)
