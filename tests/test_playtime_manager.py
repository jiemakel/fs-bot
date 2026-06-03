from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
import asyncio

import pytest
from signalbot import Context, SignalBot

from family_safety_bot.storage import PlaytimeStore
from family_safety_bot.watcher import PlaytimeManager
from tests.helpers import ADMIN_PHONE, CHILD_PHONE, build_settings, build_store


@dataclass
class FakeContext:
    """Fake signalbot Context for testing."""

    message_text: str
    sender: str
    sent_messages: list[str]

    @property
    def message(self):
        class FakeMessage:
            def __init__(self, text: str, source: str):
                self.text = text
                self.source = source

        return FakeMessage(self.message_text, self.sender)

    async def send(self, text: str) -> None:
        self.sent_messages.append(text)


@dataclass
class FakeBot:
    """Fake signalbot for testing."""

    sent: list[tuple[str, str]]
    scheduler: Any = None

    async def send(self, receiver: str, text: str, **kwargs) -> None:
        self.sent.append((receiver, text))


def _build_manager(tmp_path: Path, **settings_overrides: Any) -> tuple[PlaytimeManager, PlaytimeStore]:
    store = build_store(tmp_path)
    manager = PlaytimeManager(build_settings(tmp_path, **settings_overrides), store)
    manager.bot = cast(SignalBot, FakeBot(sent=[]))
    return manager, store


def _run_handle(manager: PlaytimeManager, ctx: FakeContext) -> None:
    asyncio.run(manager.handle(cast(Context, ctx)))


def test_playtime_manager_status_command(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, 120)

    ctx = FakeContext(message_text="status", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert store.get_bank_balance(CHILD_PHONE) == 120
    assert store.get_active_session(CHILD_PHONE) is None


def test_playtime_manager_child_request_validation(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, 120)

    ctx = FakeContext(message_text="0m", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert store.get_bank_balance(CHILD_PHONE) == 120
    assert store.get_active_session(CHILD_PHONE) is None


def test_playtime_manager_unknown_sender(tmp_path: Path) -> None:
    manager, _store = _build_manager(tmp_path)

    ctx = FakeContext(message_text="60", sender="+9999999999", sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 0


@pytest.mark.parametrize(
    ("starting_balance", "message_text", "expected_balance"),
    [
        (60, "TestChild 2h", 180),
        (60, "2h", 180),
        (60, "TestChild +30m", 90),
        (60, "TestChild -30m", 30),
        (60, "TestChild =2h", 120),
        (20, "TestChild -1h", 0),
    ],
)
def test_playtime_manager_admin_bank_commands(
    tmp_path: Path,
    starting_balance: int,
    message_text: str,
    expected_balance: int,
) -> None:
    manager, store = _build_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, starting_balance)

    ctx = FakeContext(message_text=message_text, sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert store.get_bank_balance(CHILD_PHONE) == expected_balance


def test_playtime_manager_admin_can_set_break_balance(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    now = manager._rules_by_child[CHILD_PHONE]._get_local_now()
    store.set_consumed_break_debt(CHILD_PHONE, 30, now)

    ctx = FakeContext(message_text="break TestChild 2h", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert store.get_consumed_break_debt(CHILD_PHONE)[0] == 120


def test_playtime_manager_admin_can_toggle_grant_block_mode(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)

    block_ctx = FakeContext(message_text="block", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, block_ctx)

    assert len(block_ctx.sent_messages) == 1
    assert store.is_child_block_mode_enabled(CHILD_PHONE) is True

    unblock_ctx = FakeContext(message_text="unblock", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, unblock_ctx)

    assert len(unblock_ctx.sent_messages) == 1
    assert store.is_child_block_mode_enabled(CHILD_PHONE) is False


def test_playtime_manager_admin_can_toggle_grant_block_mode_for_named_child(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)

    block_ctx = FakeContext(message_text="block TestChild", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, block_ctx)

    assert len(block_ctx.sent_messages) == 1
    assert store.is_child_block_mode_enabled(CHILD_PHONE) is True

    unblock_ctx = FakeContext(message_text="unblock TestChild", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, unblock_ctx)

    assert len(unblock_ctx.sent_messages) == 1
    assert store.is_child_block_mode_enabled(CHILD_PHONE) is False


def test_playtime_manager_admin_block_ends_active_session(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, 120)

    async def fake_grant(child_id: str, minutes: int) -> bool:
        return child_id == "child123" and minutes == 60

    manager._grant_via_ms_api = fake_grant  # type: ignore[method-assign]
    grant_ctx = FakeContext(message_text="1h", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, grant_ctx)
    assert store.get_active_session(CHILD_PHONE) is not None

    async def fake_block(child_id: str) -> bool:
        return child_id == "child123"

    manager._block_via_ms_api = fake_block  # type: ignore[method-assign]
    block_ctx = FakeContext(message_text="block", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, block_ctx)

    assert store.is_child_block_mode_enabled(CHILD_PHONE) is True
    assert store.get_active_session(CHILD_PHONE) is None
    assert len(block_ctx.sent_messages) == 1


def test_playtime_manager_child_request_denied_while_grant_block_mode_enabled(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, 120)
    store.set_child_block_mode(CHILD_PHONE, True)

    async def fake_grant(_child_id: str, _minutes: int) -> bool:
        raise AssertionError("grant API should not be called while block mode is enabled")

    manager._grant_via_ms_api = fake_grant  # type: ignore[method-assign]

    ctx = FakeContext(message_text="1h", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert store.get_bank_balance(CHILD_PHONE) == 120
    assert store.get_active_session(CHILD_PHONE) is None


def test_playtime_manager_child_request_accepts_compound_duration(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, 120)

    async def fake_grant(child_id: str, minutes: int) -> bool:
        return child_id == "child123" and minutes == 90

    manager._grant_via_ms_api = fake_grant  # type: ignore[method-assign]
    ctx = FakeContext(message_text="1h 30m", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert store.get_bank_balance(CHILD_PHONE) == 30
    active = store.get_active_session(CHILD_PHONE)
    assert active is not None
    assert active[2] == 90


def test_playtime_manager_partial_grant_applies_multiple_caps(tmp_path: Path) -> None:
    manager, store = _build_manager(
        tmp_path,
        break_balance_max_minutes=40,
        blackout_periods=[(0, "21:00", "24:00")],
    )
    rules = manager._rules_by_child[CHILD_PHONE]
    fixed_now = rules._get_local_now().replace(year=2025, month=1, day=6, hour=20, minute=30, second=0, microsecond=0)
    rules._get_local_now = lambda: fixed_now  # type: ignore[method-assign]
    store.set_bank_balance(CHILD_PHONE, 20)
    store.set_consumed_break_debt(CHILD_PHONE, 10, fixed_now)

    async def fake_grant(child_id: str, minutes: int) -> bool:
        return child_id == "child123" and minutes == 20

    manager._grant_via_ms_api = fake_grant  # type: ignore[method-assign]
    ctx = FakeContext(message_text="1h", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    active = store.get_active_session(CHILD_PHONE)
    assert active is not None
    assert active[2] == 20
    assert store.get_bank_balance(CHILD_PHONE) == 0


def test_playtime_manager_child_end_applies_block_then_ends_session(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    rules = manager._rules_by_child[CHILD_PHONE]
    now = rules._get_local_now()
    store.add_session(CHILD_PHONE, now - timedelta(minutes=5), 60)

    async def fake_block(child_id: str) -> bool:
        return child_id == "child123"

    manager._block_via_ms_api = fake_block  # type: ignore[method-assign]
    ctx = FakeContext(message_text="end", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert store.get_active_session(CHILD_PHONE) is None


def test_playtime_manager_child_end_does_not_complete_when_block_fails(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    rules = manager._rules_by_child[CHILD_PHONE]
    now = rules._get_local_now()
    session_id = store.add_session(CHILD_PHONE, now - timedelta(minutes=5), 60)

    async def fake_block(_child_id: str) -> bool:
        return False

    manager._block_via_ms_api = fake_block  # type: ignore[method-assign]
    ctx = FakeContext(message_text="end", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    active = store.get_active_session(CHILD_PHONE)
    assert active is not None
    assert active[0] == session_id


def test_playtime_manager_child_profile_switch_affects_limits(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)

    env_payload = (
        "WEEKLY_ADDITION_TIME=14h MAX_BANK_TIME=1h BREAK_BALANCE_MAX_TIME=3h BREAK_RECOVERY_RATE=3.0 "
        "BLACKOUT_PERIOD_MON= BLACKOUT_PERIOD_TUE= BLACKOUT_PERIOD_WED= "
        "BLACKOUT_PERIOD_THU= BLACKOUT_PERIOD_FRI= BLACKOUT_PERIOD_SAT= BLACKOUT_PERIOD_SUN="
    )
    _run_handle(
        manager,
        FakeContext(message_text=f"profile define strict {env_payload}", sender=ADMIN_PHONE, sent_messages=[]),
    )
    _run_handle(manager, FakeContext(message_text="profile use strict TestChild", sender=ADMIN_PHONE, sent_messages=[]))

    ctx = FakeContext(message_text="TestChild 2h", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert store.get_bank_balance(CHILD_PHONE) == 60



def test_playtime_manager_profile_use_default_assigns_default_profile_to_child(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)

    strict_payload = (
        "WEEKLY_ADDITION_TIME=14h MAX_BANK_TIME=1h BREAK_BALANCE_MAX_TIME=3h BREAK_RECOVERY_RATE=3.0 "
        "BLACKOUT_PERIOD_MON= BLACKOUT_PERIOD_TUE= BLACKOUT_PERIOD_WED= "
        "BLACKOUT_PERIOD_THU= BLACKOUT_PERIOD_FRI= BLACKOUT_PERIOD_SAT= BLACKOUT_PERIOD_SUN="
    )
    _run_handle(
        manager,
        FakeContext(message_text=f"profile define strict {strict_payload}", sender=ADMIN_PHONE, sent_messages=[]),
    )
    _run_handle(manager, FakeContext(message_text="profile use strict TestChild", sender=ADMIN_PHONE, sent_messages=[]))
    _run_handle(manager, FakeContext(message_text="profile use default TestChild", sender=ADMIN_PHONE, sent_messages=[]))

    ctx = FakeContext(message_text="TestChild 2h", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert store.get_bank_balance(CHILD_PHONE) == 120
    assert store.get_active_rule_profile_name_for_child(CHILD_PHONE) == "default"


def test_child_activity_claim_stored_and_bank_unchanged(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, 60)

    ctx = FakeContext(message_text="30m played guitar", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert store.get_bank_balance(CHILD_PHONE) == 60
    assert store.get_active_session(CHILD_PHONE) is None
    claims = store.get_pending_claims(CHILD_PHONE)
    assert len(claims) == 1
    assert claims[0].claimed_minutes == 30
    assert claims[0].description == "played guitar"


def test_admin_claims_lists_pending(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    from datetime import datetime, timezone
    store.add_activity_claim(CHILD_PHONE, 30, "played guitar", datetime.now(timezone.utc))
    store.add_activity_claim(CHILD_PHONE, 60, "cleaned room", datetime.now(timezone.utc))

    ctx = FakeContext(message_text="claims", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert "played guitar" in ctx.sent_messages[0]
    assert "cleaned room" in ctx.sent_messages[0]
    assert "30" in ctx.sent_messages[0]


def test_admin_ack_grants_total_and_clears_claims(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, 60)
    from datetime import datetime, timezone
    store.add_activity_claim(CHILD_PHONE, 30, "played guitar", datetime.now(timezone.utc))
    store.add_activity_claim(CHILD_PHONE, 60, "cleaned room", datetime.now(timezone.utc))

    ctx = FakeContext(message_text="ack", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert store.get_bank_balance(CHILD_PHONE) == 150  # 60 + 30 + 60
    assert store.get_pending_claims(CHILD_PHONE) == []


def test_admin_ack_no_claims_sends_message(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)

    ctx = FakeContext(message_text="ack", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert store.get_pending_claims(CHILD_PHONE) == []


def test_admin_bank_modification_auto_handles_pending_claims(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, 60)
    from datetime import datetime, timezone
    store.add_activity_claim(CHILD_PHONE, 30, "played guitar", datetime.now(timezone.utc))

    ctx = FakeContext(message_text="TestChild 1h", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert store.get_bank_balance(CHILD_PHONE) == 120  # 60 + 60
    assert store.get_pending_claims(CHILD_PHONE) == []
    assert "played guitar" in ctx.sent_messages[0]
