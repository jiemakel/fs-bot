from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from family_safety_bot.config import BankProfile, RuleProfile
from family_safety_bot.rules import PlaytimeRules
from tests.helpers import CHILD_PHONE, build_settings, build_store

def _build_rules(tmp_path: Path, **settings_overrides: Any) -> PlaytimeRules:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path, **settings_overrides)
    return PlaytimeRules(
        CHILD_PHONE,
        settings,
        store,
        profile_provider=lambda: settings.default_rule_profile,
    )


class RecordingI18n:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def msg(self, key: str, **kwargs: Any) -> str:
        self.calls.append((key, kwargs))
        return key


def test_playtime_rules_blackout_multiple_periods_same_day(tmp_path: Path) -> None:
    rules = _build_rules(tmp_path, blackout_periods=[(0, "08:00", "09:00"), (0, "20:30", "21:15")])

    is_blackout, _reason = rules._is_in_blackout_period(datetime(2025, 1, 6, 8, 30))
    assert is_blackout is True

    is_blackout, _ = rules._is_in_blackout_period(datetime(2025, 1, 6, 12, 0))
    assert is_blackout is False

    is_blackout, _reason = rules._is_in_blackout_period(datetime(2025, 1, 6, 21, 0))
    assert is_blackout is True


def test_playtime_rules_blackout_until_midnight_24_00(tmp_path: Path) -> None:
    rules = _build_rules(tmp_path, blackout_periods=[(0, "20:30", "24:00")])

    is_blackout, _ = rules._is_in_blackout_period(datetime(2025, 1, 6, 20, 29))
    assert is_blackout is False

    is_blackout, _ = rules._is_in_blackout_period(datetime(2025, 1, 6, 20, 30))
    assert is_blackout is True

    is_blackout, _ = rules._is_in_blackout_period(datetime(2025, 1, 6, 23, 59))
    assert is_blackout is True

    is_blackout, _ = rules._is_in_blackout_period(datetime(2025, 1, 7, 0, 0))
    assert is_blackout is False


def test_playtime_rules_upcoming_blackout_caps_grant_instead_of_denial(tmp_path: Path) -> None:
    rules = _build_rules(tmp_path, blackout_periods=[(0, "20:30", "24:00")])
    rules._get_local_now = lambda: datetime(2025, 1, 6, 20, 0, tzinfo=timezone.utc)  # type: ignore[method-assign]
    rules._store.set_bank_balance(CHILD_PHONE, 300)

    decision = rules.evaluate_request(90)
    assert decision.allowed is True
    assert decision.minutes_granted == 30


def test_playtime_rules_upcoming_blackout_next_day_caps_grant(tmp_path: Path) -> None:
    rules = _build_rules(tmp_path, blackout_periods=[(1, "00:00", "08:00")])
    rules._get_local_now = lambda: datetime(2025, 1, 6, 23, 30, tzinfo=timezone.utc)  # type: ignore[method-assign]
    rules._store.set_bank_balance(CHILD_PHONE, 300)

    decision = rules.evaluate_request(90)
    assert decision.allowed is True
    assert decision.minutes_granted == 30


def test_playtime_rules_partial_grant_applies_active_limiters(tmp_path: Path) -> None:
    rules = _build_rules(
        tmp_path,
        blackout_periods=[(0, "21:00", "24:00")],
    )
    now = datetime(2025, 1, 6, 20, 30, tzinfo=timezone.utc)
    rules._get_local_now = lambda: now  # type: ignore[method-assign]
    rules._store.set_bank_balance(CHILD_PHONE, 20)

    decision = rules.evaluate_request(60)
    assert decision.allowed is True
    assert decision.minutes_granted == 20


def test_playtime_rules_basic_request(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)

    store.set_bank_balance(CHILD_PHONE, 300)

    decision = rules.evaluate_request(60)
    assert decision.allowed is True
    assert decision.minutes_granted == 60

    session_id, _message = rules.grant_playtime(60)
    assert session_id > 0

    assert store.get_bank_balance(CHILD_PHONE) == 240
    assert store.get_bank_balance(CHILD_PHONE, "recovery") == 120
    active = store.get_active_session(CHILD_PHONE)
    assert active is not None
    assert active[0] == session_id
    assert active[2] == 60


def test_playtime_rules_exceeds_bank_balance(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)

    store.set_bank_balance(CHILD_PHONE, 30)

    decision = rules.evaluate_request(60)
    assert decision.allowed is True
    assert decision.minutes_granted == 30


def test_playtime_rules_secondary_bank_caps_grants(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(
        tmp_path,
        banks={
            "default": BankProfile("default", 180, 300),
            "weekly": BankProfile("weekly", 90, 90),
        },
    )
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)
    now = datetime(2025, 1, 8, 12, 0, tzinfo=timezone.utc)
    rules._get_local_now = lambda: now  # type: ignore[method-assign]
    store.set_bank_balance(CHILD_PHONE, 300)
    store.set_bank_balance(CHILD_PHONE, 30, "weekly")

    decision = rules.evaluate_request(60)

    assert decision.allowed is True
    assert decision.minutes_granted == 30
    assert "weekly" in decision.reason


def test_playtime_rules_ending_early_refunds_every_bank(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(
        tmp_path,
        banks={
            "default": BankProfile("default", 180, 300),
            "weekly": BankProfile("weekly", 90, 90),
        },
    )
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)
    start = datetime(2025, 1, 8, 12, 0, tzinfo=timezone.utc)
    rules._get_local_now = lambda: start  # type: ignore[method-assign]
    store.set_bank_balance(CHILD_PHONE, 300)
    store.set_bank_balance(CHILD_PHONE, 90, "weekly")

    decision = rules.evaluate_request(90)
    rules.grant_playtime(decision.minutes_granted)
    assert rules.evaluate_request(120).allowed is False

    rules._get_local_now = lambda: start + timedelta(minutes=30)  # type: ignore[method-assign]
    rules.complete_session()
    assert rules.evaluate_request(60).minutes_granted == 60
    assert store.get_bank_balance(CHILD_PHONE, "weekly") == 60


def test_playtime_rules_blackout_full_day_period(tmp_path: Path) -> None:
    rules = _build_rules(tmp_path, blackout_periods=[(0, "00:00", "24:00"), (1, "00:00", "24:00")])

    monday_noon = datetime(2025, 1, 6, 12, 0)
    is_blackout, _reason = rules._is_in_blackout_period(monday_noon)
    assert is_blackout is True


def test_recovery_bank_refills_only_after_complete_break(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)
    store.set_bank_balance(CHILD_PHONE, 300)
    base = datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc)
    store.set_bank_balance(CHILD_PHONE, 90, "recovery", base)

    rules._get_local_now = lambda: base + timedelta(minutes=20)  # type: ignore[method-assign]
    rules.evaluate_request(60)
    assert store.get_bank_balance(CHILD_PHONE, "recovery") == 90

    rules._get_local_now = lambda: base + timedelta(minutes=30)  # type: ignore[method-assign]
    rules.evaluate_request(60)
    assert store.get_bank_balance(CHILD_PHONE, "recovery") == 180


def test_natural_session_end_starts_recovery_bank_break(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)
    start = datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc)
    rules._get_local_now = lambda: start  # type: ignore[method-assign]
    store.set_bank_balance(CHILD_PHONE, 300)
    rules.grant_playtime(60)

    rules._get_local_now = lambda: start + timedelta(minutes=79)  # type: ignore[method-assign]
    rules.check_automatic_updates()
    assert store.get_bank_balance(CHILD_PHONE, "recovery") == 120

    rules._get_local_now = lambda: start + timedelta(minutes=80)  # type: ignore[method-assign]
    rules.check_automatic_updates()
    assert store.get_bank_balance(CHILD_PHONE, "recovery") == 180


def test_recovery_bank_completion_notification_is_one_shot(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path)
    i18n = RecordingI18n()
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile, i18n=i18n)  # type: ignore[arg-type]
    base = datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc)
    store.set_bank_balance(CHILD_PHONE, 90, "recovery", base)
    rules._get_local_now = lambda: base + timedelta(minutes=30)  # type: ignore[method-assign]

    assert len(rules.check_automatic_updates()) == 1
    assert rules.check_automatic_updates() == []


def test_recovery_break_progress_is_restored_after_quick_stop(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)
    base = datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc)
    grant_at = base + timedelta(minutes=20)
    stop_at = grant_at + timedelta(seconds=30)
    store.set_bank_balance(CHILD_PHONE, 300)
    store.set_bank_balance(CHILD_PHONE, 90, "recovery", base)

    rules._get_local_now = lambda: grant_at  # type: ignore[method-assign]
    rules.grant_playtime(10)
    assert store.get_recovery_interruption(CHILD_PHONE) is not None

    rules._get_local_now = lambda: stop_at  # type: ignore[method-assign]
    rules.complete_session()
    balance, recovery_started = store.get_bank_state(CHILD_PHONE, "recovery")
    assert balance == 90
    assert recovery_started == base

    rules._get_local_now = lambda: stop_at + timedelta(minutes=10)  # type: ignore[method-assign]
    rules.evaluate_request(1)
    assert store.get_bank_balance(CHILD_PHONE, "recovery") == 180


def test_recovery_break_progress_resets_after_grace_expires(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)
    base = datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc)
    grant_at = base + timedelta(minutes=20)
    stop_at = grant_at + timedelta(minutes=2, seconds=1)
    store.set_bank_balance(CHILD_PHONE, 300)
    store.set_bank_balance(CHILD_PHONE, 90, "recovery", base)

    rules._get_local_now = lambda: grant_at  # type: ignore[method-assign]
    rules.grant_playtime(10)
    rules._get_local_now = lambda: stop_at  # type: ignore[method-assign]
    rules.complete_session()
    balance, recovery_started = store.get_bank_state(CHILD_PHONE, "recovery")
    assert balance == 88
    assert recovery_started == stop_at


def test_playtime_rules_active_session_request_uses_surplus_only(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)

    now = rules._get_local_now()
    store.set_bank_balance(CHILD_PHONE, 200)
    session_id = store.add_session(CHILD_PHONE, now - timedelta(minutes=30), 60)

    decision = rules.evaluate_request(40)
    assert decision.allowed is True
    assert decision.minutes_granted == 40

    _sid, _msg = rules.grant_playtime(decision.minutes_granted)
    assert _sid == session_id
    assert store.get_bank_balance(CHILD_PHONE) == 190  # +10 surplus only

    active = store.get_active_session(CHILD_PHONE)
    assert active is not None
    assert active[2] == 70  # original 60 + 10 surplus


def test_playtime_rules_active_session_smaller_request_no_refund(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)

    now = rules._get_local_now()
    store.set_bank_balance(CHILD_PHONE, 200)
    session_id = store.add_session(CHILD_PHONE, now - timedelta(minutes=10), 60)

    decision = rules.evaluate_request(20)
    assert decision.allowed is False
    assert store.get_bank_balance(CHILD_PHONE) == 200  # no subtraction and no refund

    active = store.get_active_session(CHILD_PHONE)
    assert active is not None
    assert active[0] == session_id
    assert active[2] == 60  # unchanged


def test_playtime_rules_status_reports_each_bank_against_its_maximum(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(
        tmp_path,
        banks={"default": BankProfile("default", 60, 100)},
    )
    i18n = RecordingI18n()
    rules = PlaytimeRules(
        CHILD_PHONE,
        settings,
        store,
        profile_provider=lambda: settings.default_rule_profile,
        i18n=i18n,  # type: ignore[arg-type]
    )
    now = datetime(2025, 1, 8, 12, 0, tzinfo=timezone.utc)
    rules._get_local_now = lambda: now  # type: ignore[method-assign]
    store.set_bank_balance(CHILD_PHONE, 60)

    rules.get_status()

    pace_call = next(kwargs for key, kwargs in i18n.calls if key == "rules.status_bank_avg_pace")
    assert pace_call["balance_share"] == "60.0%"


def test_playtime_rules_add_to_bank(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path, banks={"default": BankProfile("default", 60, 200)})
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)

    _result = rules.add_to_bank(30)
    assert store.get_bank_balance(CHILD_PHONE) == 30

    _result = rules.add_to_bank(200)
    assert store.get_bank_balance(CHILD_PHONE) == 200


def test_playtime_rules_activity_addition_targets_first_configured_bank(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(
        tmp_path,
        banks={
            "earned": BankProfile("earned", 60, 200),
            "weekly": BankProfile("weekly", 90, 90),
        },
    )
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)

    rules.add_to_bank(30)

    assert store.get_bank_balance(CHILD_PHONE, "earned") == 30
    assert store.get_bank_balance(CHILD_PHONE, "weekly") == 0


def test_playtime_rules_rollover_updates_every_bank(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(
        tmp_path,
        banks={
            "earned": BankProfile("earned", 60, 200),
            "weekly": BankProfile("weekly", 90, 90),
        },
    )
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)
    store.set_bank_balance(CHILD_PHONE, 170, "earned")
    store.set_bank_balance(CHILD_PHONE, 20, "weekly")

    rules.rollover_week()

    assert store.get_bank_balance(CHILD_PHONE, "earned") == 200
    assert store.get_bank_balance(CHILD_PHONE, "weekly") == 90


def test_bank_balance_persists_on_profile_switch_until_rollover(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path)
    high_profile = settings.default_rule_profile
    low_profile = RuleProfile(
        name="low",
        banks={"default": BankProfile("default", 30, 90)},
        blackout_periods=[],
    )
    active_profile = [high_profile]
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: active_profile[0])
    store.set_bank_balance(CHILD_PHONE, 200)

    active_profile[0] = low_profile
    rules.get_status()
    assert store.get_bank_balance(CHILD_PHONE) == 200

    rules.rollover_week()

    assert store.get_bank_balance(CHILD_PHONE) == 90
    decision = rules.evaluate_request(30)
    assert decision.allowed is True


def test_playtime_rules_rollover_week(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)

    store.set_bank_balance(CHILD_PHONE, 500)
    store.set_bank_balance(CHILD_PHONE, 60, "recovery")

    _result = rules.rollover_week()

    assert store.get_bank_balance(CHILD_PHONE) == 1340
    assert store.get_bank_balance(CHILD_PHONE, "recovery") == 60


def test_playtime_rules_set_bank(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path, banks={"default": BankProfile("default", 60, 200)})
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)
    store.set_bank_balance(CHILD_PHONE, 30)

    _result = rules.set_bank(90)
    assert store.get_bank_balance(CHILD_PHONE) == 90

    _result_capped = rules.set_bank(999)
    assert store.get_bank_balance(CHILD_PHONE) == 200
