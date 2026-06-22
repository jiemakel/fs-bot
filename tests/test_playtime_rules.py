from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
        accrued_playtime_max_minutes=40,
        blackout_periods=[(0, "21:00", "24:00")],
    )
    now = datetime(2025, 1, 6, 20, 30, tzinfo=timezone.utc)
    rules._get_local_now = lambda: now  # type: ignore[method-assign]
    rules._store.set_bank_balance(CHILD_PHONE, 20)
    rules._store.set_accrued_playtime(CHILD_PHONE, 10, now)

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


def test_playtime_rules_accrued_playtime_maxed(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)

    now = datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc)
    rules._get_local_now = lambda: now  # type: ignore[method-assign]
    store.set_bank_balance(CHILD_PHONE, 500)
    session_id = store.add_session(CHILD_PHONE, now - timedelta(minutes=10), 180)
    store.set_accrued_playtime(CHILD_PHONE, 180, now)

    decision = rules.evaluate_request(60)
    assert decision.allowed is False
    assert decision.minutes_granted == 0

    store.complete_session(session_id, now)
    rules._get_local_now = lambda: now + timedelta(minutes=1)  # type: ignore[method-assign]
    decision2 = rules.evaluate_request(60)
    assert decision2.minutes_granted <= 3


def test_playtime_rules_blackout_full_day_period(tmp_path: Path) -> None:
    rules = _build_rules(tmp_path, blackout_periods=[(0, "00:00", "24:00"), (1, "00:00", "24:00")])

    monday_noon = datetime(2025, 1, 6, 12, 0)
    is_blackout, _reason = rules._is_in_blackout_period(monday_noon)
    assert is_blackout is True


def test_playtime_rules_accrued_playtime_recovery(tmp_path: Path) -> None:
    """Recovery is only granted at end of a full rest period (all-or-nothing)."""
    store = build_store(tmp_path)
    settings = build_settings(tmp_path)  # break_recovery_rate=3.0
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)

    store.set_bank_balance(CHILD_PHONE, 300)

    # 90 min debt requires 90/3 = 30 min rest.
    base = datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc)
    store.set_accrued_playtime(CHILD_PHONE, 90, base)

    # After 20 min of rest (< 30 min needed): no recovery yet.
    rules._get_local_now = lambda: base + timedelta(minutes=20)  # type: ignore[method-assign]
    rules.evaluate_request(60)
    balance, _, rest_acc = store.get_accrued_playtime(CHILD_PHONE)
    assert balance == 90
    assert int(rest_acc) == 20

    # After 30 min of rest (full period complete): debt cleared.
    rules._get_local_now = lambda: base + timedelta(minutes=30)  # type: ignore[method-assign]
    rules.evaluate_request(60)
    balance, _, rest_acc = store.get_accrued_playtime(CHILD_PHONE)
    assert balance == 0
    assert rest_acc == 0.0


def test_playtime_rules_recovery_completion_message_is_one_shot(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path, break_recovery_rate=3.0)
    i18n = RecordingI18n()
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile, i18n=i18n)  # type: ignore[arg-type]

    base = datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc)
    store.set_accrued_playtime(CHILD_PHONE, 90, base)
    rules._get_local_now = lambda: base + timedelta(minutes=30)  # type: ignore[method-assign]

    message = rules.check_recovery_completion()

    assert isinstance(message, str)
    balance, _, rest_acc = store.get_accrued_playtime(CHILD_PHONE)
    assert balance == 0
    assert rest_acc == 0.0
    assert rules.check_recovery_completion() is None
    recovery_calls = [kwargs for key, kwargs in i18n.calls if key == "rules.recovery_completed"]
    assert len(recovery_calls) == 1
    assert set(recovery_calls[0]) == {"recovery", "accrued_playtime"}
    assert recovery_calls[0]["recovery"] != recovery_calls[0]["accrued_playtime"]


def test_playtime_rules_recovery_abort_can_be_resumed_by_quick_stop(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path, break_recovery_rate=3.0)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)

    base = datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc)
    grant_at = base + timedelta(minutes=20)
    stop_at = grant_at + timedelta(seconds=30)
    store.set_bank_balance(CHILD_PHONE, 300)
    store.set_accrued_playtime(CHILD_PHONE, 90, base)

    rules._get_local_now = lambda: grant_at  # type: ignore[method-assign]
    session_id, grant_message = rules.grant_playtime(10)

    assert session_id > 0
    assert isinstance(grant_message, str)
    abort = store.get_recovery_abort(CHILD_PHONE)
    assert abort is not None
    saved_rest, abort_time = abort
    assert int(saved_rest) == 20
    assert abort_time == grant_at

    rules._get_local_now = lambda: stop_at  # type: ignore[method-assign]
    complete_message = rules.complete_session()

    assert isinstance(complete_message, str)
    assert store.get_recovery_abort(CHILD_PHONE) is None
    balance, last_update, rest_acc = store.get_accrued_playtime(CHILD_PHONE)
    assert balance == 90
    assert last_update == stop_at
    assert int(rest_acc) == 20

    rules._get_local_now = lambda: stop_at + timedelta(minutes=10)  # type: ignore[method-assign]
    rules.evaluate_request(1)

    balance, _, rest_acc = store.get_accrued_playtime(CHILD_PHONE)
    assert balance == 0
    assert rest_acc == 0.0


def test_playtime_rules_recovery_abort_expired_grace_does_not_restore_rest(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path, break_recovery_rate=3.0)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)

    base = datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc)
    grant_at = base + timedelta(minutes=20)
    stop_at = grant_at + timedelta(minutes=2)
    store.set_bank_balance(CHILD_PHONE, 300)
    store.set_accrued_playtime(CHILD_PHONE, 90, base)

    rules._get_local_now = lambda: grant_at  # type: ignore[method-assign]
    _session_id, grant_message = rules.grant_playtime(10)
    assert isinstance(grant_message, str)
    assert store.get_recovery_abort(CHILD_PHONE) is not None

    rules._get_local_now = lambda: stop_at  # type: ignore[method-assign]
    complete_message = rules.complete_session()

    assert isinstance(complete_message, str)
    assert store.get_recovery_abort(CHILD_PHONE) is None
    balance, last_update, rest_acc = store.get_accrued_playtime(CHILD_PHONE)
    assert balance == 92
    assert last_update == stop_at
    assert rest_acc == 0.0


def test_playtime_rules_natural_session_end_starts_break_recovery(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)

    start = datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc)
    store.add_session(CHILD_PHONE, start, 60)
    store.set_accrued_playtime(CHILD_PHONE, 0, start)

    now = start + timedelta(minutes=90)
    rules._get_local_now = lambda: now  # type: ignore[method-assign]

    assert rules.has_active_session() is False
    balance, _, _rest = store.get_accrued_playtime(CHILD_PHONE)
    assert balance == 0


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


def test_playtime_rules_status_active_recovery_uses_accrued_debt_only(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path, break_recovery_rate=3.0)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)

    start = datetime(2025, 1, 6, 10, 0, tzinfo=timezone.utc)
    now = start + timedelta(minutes=30)
    rules._get_local_now = lambda: now  # type: ignore[method-assign]

    store.add_session(CHILD_PHONE, start, 60)
    store.set_accrued_playtime(CHILD_PHONE, 0, start)

    status = rules.get_status()
    balance, _, _rest = store.get_accrued_playtime(CHILD_PHONE)
    assert balance == 30
    assert status


def test_playtime_rules_add_to_bank(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path, max_bank_minutes=200)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)

    _result = rules.add_to_bank(30)
    assert store.get_bank_balance(CHILD_PHONE) == 30

    _result = rules.add_to_bank(200)
    assert store.get_bank_balance(CHILD_PHONE) == 200


def test_playtime_rules_rollover_week(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)

    store.set_bank_balance(CHILD_PHONE, 500)

    _result = rules.rollover_week()

    assert store.get_bank_balance(CHILD_PHONE) == 1340


def test_playtime_rules_set_bank(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path, max_bank_minutes=200)
    rules = PlaytimeRules(CHILD_PHONE, settings, store, profile_provider=lambda: settings.default_rule_profile)
    store.set_bank_balance(CHILD_PHONE, 30)

    _result = rules.set_bank(90)
    assert store.get_bank_balance(CHILD_PHONE) == 90

    _result_capped = rules.set_bank(999)
    assert store.get_bank_balance(CHILD_PHONE) == 200


