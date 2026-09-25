from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
import asyncio
import json
import sqlite3

import pytest
from signalbot import SendMessage, SignalBot

from family_safety_bot.config import BankProfile, Child
from family_safety_bot.ms_family import MicrosoftFamilyApi
from family_safety_bot.storage import PlaytimeStore
from family_safety_bot.watcher import PlaytimeManager
from tests.helpers import ADMIN_PHONE, CHILD_PHONE, build_settings, build_store


@dataclass
class FakeMessage:
    text: str
    source: str
    mentions: list[Any] = field(default_factory=list)
    quote: Any = None

    @property
    def source_number(self) -> str:
        return self.source


@dataclass
class FakeContext:
    """Fake signalbot DataMessageContext for testing."""
    message_text: str
    sender: str
    sent_messages: list[str]
    mentions: list[Any] = field(default_factory=list)
    quote: Any = None

    @property
    def message(self) -> FakeMessage:
        return FakeMessage(self.message_text, self.sender, self.mentions, self.quote)

    async def send(self, message: SendMessage) -> None:
        self.sent_messages.append(message.text or "")


@dataclass
class FakeBot:
    """Fake signalbot for testing."""

    sent: list[tuple[str, str]]
    scheduler: Any = None

    @property
    def messages(self) -> "FakeBot":
        return self

    async def send(self, message: SendMessage, receiver: str) -> None:
        self.sent.append((receiver, message.text or ""))


class AuthenticatedMicrosoftFamilyApi:
    async def has_valid_session(self) -> bool:
        return True

    async def ensure_authenticated(self, **_kwargs: Any) -> None:
        pass


def _build_manager(tmp_path: Path, **settings_overrides: Any) -> tuple[PlaytimeManager, PlaytimeStore]:
    store = build_store(tmp_path)
    settings = build_settings(tmp_path, **settings_overrides)
    if settings.default_rule_profile is not None:
        store.upsert_rule_profile(settings.default_rule_profile)
        for child_phone in settings.children:
            store.set_active_rule_profile_name_for_child(child_phone, settings.default_rule_profile.name)
    manager = PlaytimeManager(settings, store)
    manager.bot = cast(SignalBot, FakeBot(sent=[]))
    manager._ms_api = cast(MicrosoftFamilyApi, AuthenticatedMicrosoftFamilyApi())
    manager._ms_api_email = "test@example.com"
    return manager, store


def _run_handle(manager: PlaytimeManager, ctx: FakeContext) -> None:
    asyncio.run(manager.handle_data_message(ctx))  # type: ignore[arg-type]


def _profile_json(banks: dict[str, dict[str, str]] | None = None) -> str:
    return json.dumps(
        {
            "banks": banks
            or {
                "default": {"weekly_addition": "14h", "max_balance": "1h"},
                "recovery": {"recovery_rate": 3.0, "max_balance": "3h"},
            },
            "blackouts": [],
        }
    )


def test_playtime_manager_parses_day_bank_admin_amount(tmp_path: Path) -> None:
    manager, _store = _build_manager(tmp_path)

    parsed = manager._parse_admin_bank_payload("weekdays =3d")

    assert parsed is not None
    _children, bank_name, amount, is_relative = parsed
    assert bank_name == "weekdays"
    assert amount == 3
    assert is_relative is False


def test_admin_status_authenticates_with_initiating_admin_account_and_lists_children(
    tmp_path: Path,
) -> None:
    second_phone = "+1234567892"
    second_admin = "+1234567893"
    children = {
        CHILD_PHONE: Child(CHILD_PHONE, "child123", "TestChild"),
        second_phone: Child(second_phone, "child456", "SecondChild"),
    }
    manager, _store = _build_manager(
        tmp_path,
        children=children,
        signal_admins=[ADMIN_PHONE, second_admin],
        admin_ms_emails={
            ADMIN_PHONE: "first@example.com",
            second_admin: "second@example.com",
        },
    )
    authentication_checks = 0

    class FakeMicrosoftFamilyApi:
        async def has_valid_session(self) -> bool:
            return False

        async def ensure_authenticated(self, **kwargs: Any) -> None:
            nonlocal authentication_checks
            authentication_checks += 1
            challenge_handler = kwargs["mfa_challenge_handler"]
            await challenge_handler("STATUS42", 60)

    created_for: list[str] = []

    def create_ms_api(email: str) -> MicrosoftFamilyApi:
        created_for.append(email)
        return cast(MicrosoftFamilyApi, FakeMicrosoftFamilyApi())

    manager._ms_api = None
    manager._ms_api_email = None
    manager._create_ms_api = create_ms_api  # type: ignore[method-assign]

    ctx = FakeContext(message_text="status", sender=second_admin, sent_messages=[])
    _run_handle(manager, ctx)

    assert authentication_checks == 1
    assert created_for == ["second@example.com"]
    assert "STATUS42" in ctx.sent_messages[0]
    assert "authentication is working" in ctx.sent_messages[1]
    assert len(ctx.sent_messages) == len(children) + 2


def test_admin_status_reuses_valid_shared_session_from_another_admin(tmp_path: Path) -> None:
    second_admin = "+1234567893"
    manager, _store = _build_manager(
        tmp_path,
        signal_admins=[ADMIN_PHONE, second_admin],
        admin_ms_emails={
            ADMIN_PHONE: "first@example.com",
            second_admin: "second@example.com",
        },
    )

    def unexpected_new_client(_email: str) -> MicrosoftFamilyApi:
        raise AssertionError("A valid shared session must be reused")

    manager._ms_api_email = "first@example.com"
    manager._create_ms_api = unexpected_new_client  # type: ignore[method-assign]
    ctx = FakeContext(message_text="status", sender=second_admin, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert "[TestChild]" in ctx.sent_messages[0]


def test_child_command_reports_stale_authentication_without_starting_challenge(
    tmp_path: Path,
) -> None:
    manager, _store = _build_manager(tmp_path)
    interactive_auth_attempted = False

    class StaleMicrosoftFamilyApi:
        async def has_valid_session(self) -> bool:
            return False

        async def ensure_authenticated(self, **kwargs: Any) -> None:
            nonlocal interactive_auth_attempted
            interactive_auth_attempted = kwargs["mfa_challenge_handler"] is not None
            raise ValueError("Authenticator approval required")

    manager._ms_api = cast(MicrosoftFamilyApi, StaleMicrosoftFamilyApi())
    manager._ms_api_email = "test@example.com"
    ctx = FakeContext(message_text="status", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert interactive_auth_attempted is False
    assert len(ctx.sent_messages) == 1
    assert "Ask an admin" in ctx.sent_messages[0]
    assert "[TestChild]" not in ctx.sent_messages[0]


def test_playtime_manager_status_includes_pending_activity_claims(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, 120)
    store.add_activity_claim(CHILD_PHONE, 30, "30m played guitar", datetime.now(timezone.utc))
    store.add_activity_claim(CHILD_PHONE, 60, "1h cleaned room", datetime.now(timezone.utc))

    ctx = FakeContext(message_text="status", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert "Pending claims for TestChild (2):" in ctx.sent_messages[0]
    assert "30m played guitar" in ctx.sent_messages[0]
    assert "1h cleaned room" in ctx.sent_messages[0]
    assert "Total: 1h 30m" in ctx.sent_messages[0]
    assert store.get_bank_balance(CHILD_PHONE) == 120
    assert store.get_active_session(CHILD_PHONE) is None


@pytest.mark.parametrize("outcome", ["granted", "blocked", "empty_bank", "no_profile", "invalid", "api_failed", "api_error"])
def test_playtime_request_includes_pending_claims(tmp_path: Path, outcome: str) -> None:
    manager, store = _build_two_child_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, 0 if outcome == "empty_bank" else 120)
    submitted_at = datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc)
    store.add_activity_claim(CHILD_PHONE, 30, "30m played guitar", submitted_at)
    store.mark_all_claims_handled(CHILD_PHONE)
    store.add_activity_claim(CHILD_PHONE, 60, "1h cleaned room", submitted_at)
    store.add_activity_claim(CHILD_PHONE, 30, "30m went for a walk", submitted_at)
    store.add_activity_claim(SECOND_PHONE, 90, "1h30m other child's activity", submitted_at)
    if outcome == "blocked":
        store.set_child_block_mode(CHILD_PHONE, True)
    elif outcome == "no_profile":
        manager._active_profile_name_by_child.pop(CHILD_PHONE)

    async def fake_grant(_child_id: str, _minutes: int, _mfa_handler: Any = None) -> bool:
        assert outcome in {"granted", "api_failed", "api_error"}
        if outcome == "api_error":
            raise RuntimeError("API unavailable")
        return outcome == "granted"

    manager._grant_via_ms_api = fake_grant  # type: ignore[method-assign]
    ctx = FakeContext(message_text="0m" if outcome == "invalid" else "30m", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    response = ctx.sent_messages[0]
    assert "Pending claims for TestChild (2):" in response
    assert "1h cleaned room (submitted 2025-01-06 12:00)" in response
    assert "30m went for a walk" in response
    assert "Total: 1h 30m" in response
    assert "played guitar" not in response
    assert "other child's activity" not in response
    assert len(store.get_pending_claims(CHILD_PHONE)) == 2
    assert (store.get_active_session(CHILD_PHONE) is not None) == (outcome == "granted")
    expected_balance = 0 if outcome == "empty_bank" else 90 if outcome == "granted" else 120
    assert store.get_bank_balance(CHILD_PHONE) == expected_balance


def test_playtime_request_without_pending_claims_omits_overview(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, 120)

    async def fake_grant(_child_id: str, _minutes: int, _mfa_handler: Any = None) -> bool:
        return True

    manager._grant_via_ms_api = fake_grant  # type: ignore[method-assign]
    ctx = FakeContext(message_text="30m", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert "claims" not in ctx.sent_messages[0].lower()
    assert store.get_active_session(CHILD_PHONE) is not None


def test_admin_playtime_request_announces_authenticator_challenge_details(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, 120)

    async def fake_grant(_child_id: str, _minutes: int, mfa_handler: Any = None) -> bool:
        assert mfa_handler is not None
        await mfa_handler("CIKJ5", 60)
        return True

    manager._grant_via_ms_api = fake_grant  # type: ignore[method-assign]
    ctx = FakeContext(
        message_text="request TestChild 30m",
        sender=ADMIN_PHONE,
        sent_messages=[],
    )
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 2
    assert "granting 30m to TestChild" in ctx.sent_messages[0]
    assert "CIKJ5" in ctx.sent_messages[0]
    assert "60s" in ctx.sent_messages[0]
    assert "Granted 30m" in ctx.sent_messages[1]


def test_playtime_manager_unknown_sender(tmp_path: Path) -> None:
    manager, _store = _build_manager(tmp_path)

    ctx = FakeContext(message_text="60", sender="+9999999999", sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 0


@pytest.mark.parametrize(
    ("message_text", "mentions", "quote"),
    [
        ("can I play after dinner?", [], None),
        (
            "I have 30 different thoughts about the schedule and this is not really a bot command today",
            [],
            None,
        ),
        ("can I have 30 please @Admin", ["admin-uuid"], None),
        ("maybe 30", [], object()),
    ],
)
def test_playtime_manager_unknown_conversation_does_not_send_help(
    tmp_path: Path,
    message_text: str,
    mentions: list[Any],
    quote: Any,
) -> None:
    manager, _store = _build_manager(tmp_path)

    ctx = FakeContext(
        message_text=message_text,
        sender=CHILD_PHONE,
        sent_messages=[],
        mentions=mentions,
        quote=quote,
    )
    _run_handle(manager, ctx)

    assert ctx.sent_messages == []


def test_playtime_manager_unknown_short_numeric_message_sends_help(tmp_path: Path) -> None:
    manager, _store = _build_manager(tmp_path)

    ctx = FakeContext(message_text="30 minutes please", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1


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


def test_playtime_manager_natural_session_completion_notifies_group(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    rules = manager._rules_by_child[CHILD_PHONE]
    base = datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc)
    rules._get_local_now = lambda: base  # type: ignore[method-assign]
    
    store.add_session(CHILD_PHONE, base, 30)
    
    # Fast-forward 40 minutes (past session end)
    rules._get_local_now = lambda: base + timedelta(minutes=40)  # type: ignore[method-assign]
    
    # Run status check scheduler task
    asyncio.run(manager._check_automatic_updates())
    
    # We should have sent a notification to the group
    bot = cast(FakeBot, manager.bot)
    assert len(bot.sent) == 1
    receiver, _message = bot.sent[0]
    assert receiver == manager._settings.signal_group_id
    
    # The session is now complete in the DB
    assert store.get_active_session(CHILD_PHONE) is None



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

    async def fake_grant(child_id: str, minutes: int, _mfa_handler: Any = None) -> bool:
        return child_id == "child123" and minutes == 60

    manager._grant_via_ms_api = fake_grant  # type: ignore[method-assign]
    grant_ctx = FakeContext(message_text="1h", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, grant_ctx)
    assert store.get_active_session(CHILD_PHONE) is not None

    async def fake_block(child_id: str, _mfa_handler: Any = None) -> bool:
        return child_id == "child123"

    manager._block_via_ms_api = fake_block  # type: ignore[method-assign]
    block_ctx = FakeContext(message_text="block", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, block_ctx)

    assert store.is_child_block_mode_enabled(CHILD_PHONE) is True
    assert store.get_active_session(CHILD_PHONE) is None
    assert len(block_ctx.sent_messages) == 1


def test_playtime_manager_child_request_accepts_compound_duration(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, 120)

    async def fake_grant(child_id: str, minutes: int, _mfa_handler: Any = None) -> bool:
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
        blackout_periods=[(0, "21:00", "24:00")],
    )
    rules = manager._rules_by_child[CHILD_PHONE]
    fixed_now = rules._get_local_now().replace(year=2025, month=1, day=6, hour=20, minute=30, second=0, microsecond=0)
    rules._get_local_now = lambda: fixed_now  # type: ignore[method-assign]
    store.set_bank_balance(CHILD_PHONE, 20)

    async def fake_grant(child_id: str, minutes: int, _mfa_handler: Any = None) -> bool:
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

    async def fake_block(child_id: str, _mfa_handler: Any = None) -> bool:
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

    async def fake_block(_child_id: str, _mfa_handler: Any = None) -> bool:
        return False

    manager._block_via_ms_api = fake_block  # type: ignore[method-assign]
    ctx = FakeContext(message_text="end", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    active = store.get_active_session(CHILD_PHONE)
    assert active is not None
    assert active[0] == session_id


def test_playtime_manager_recovery_bank_completion_sends_one_notification(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    rules = manager._rules_by_child[CHILD_PHONE]
    base = datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc)
    store.set_bank_balance(CHILD_PHONE, 90, "recovery", base)
    rules._get_local_now = lambda: base + timedelta(minutes=30)  # type: ignore[method-assign]

    asyncio.run(manager._check_automatic_updates())

    bot = cast(FakeBot, manager.bot)
    assert len(bot.sent) == 1
    receiver, _message = bot.sent[0]
    assert receiver == manager._settings.signal_group_id
    assert store.get_bank_balance(CHILD_PHONE, "recovery") == 180

    asyncio.run(manager._check_automatic_updates())
    assert len(bot.sent) == 1


def test_playtime_manager_child_profile_switch_affects_limits(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)

    profile_json = _profile_json()
    _run_handle(
        manager,
        FakeContext(message_text=f"profile define strict {profile_json}", sender=ADMIN_PHONE, sent_messages=[]),
    )
    persisted = next(profile for profile in store.list_rule_profiles() if profile.name == "strict")
    assert persisted.banks["recovery"].recovery_rate == 3.0
    _run_handle(manager, FakeContext(message_text="profile use strict TestChild", sender=ADMIN_PHONE, sent_messages=[]))

    ctx = FakeContext(message_text="TestChild 2h", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert store.get_bank_balance(CHILD_PHONE) == 60


def test_playtime_manager_defines_and_persists_finnish_day_bank_profile(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path, bot_language="fi")
    definition = {
        "banks": {
            "liikunta": {"weekly_addition": "3h", "max_balance": "30h"},
            "viikottainen": {"weekly_addition": "22h", "max_balance": "30h"},
            "arkipäivät": {
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "weekly_addition": 3,
                "max_balance": 4,
            },
            "viikonloput": {
                "days": ["sat", "sun"],
                "weekly_addition": 1,
                "max_balance": 2,
            },
            "palautuminen": {"recovery_rate": 3.0, "max_balance": "3h"},
            "palautuminen2": {"recovery_rate": 2.0, "max_balance": "6h"},
        },
        "blackouts": [
            {
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "start": "00:00",
                "end": "08:00",
            }
        ],
    }
    payload = json.dumps(definition, ensure_ascii=False)
    ctx = FakeContext(
        message_text=f"profiili määritä default {payload}",
        sender=ADMIN_PHONE,
        sent_messages=[],
    )

    _run_handle(manager, ctx)

    assert "Profiili 'default' määritetty" in ctx.sent_messages[0]
    persisted = next(profile for profile in store.list_rule_profiles() if profile.name == "default")
    assert persisted.banks["arkipäivät"].days == (0, 1, 2, 3, 4)
    assert persisted.banks["viikonloput"].days == (5, 6)


def test_playtime_manager_profile_error_includes_validation_reason(tmp_path: Path) -> None:
    manager, _store = _build_manager(tmp_path, bot_language="fi")
    ctx = FakeContext(
        message_text='profiili määritä bad {"banks": {}, "blackouts": []}',
        sender=ADMIN_PHONE,
        sent_messages=[],
    )

    _run_handle(manager, ctx)

    assert "Virheellinen profiili:" in ctx.sent_messages[0]
    assert "at least one bank" in ctx.sent_messages[0]


def test_profile_redefinition_can_change_existing_bank_from_time_to_day_bank(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path, bot_language="fi")
    original = {
        "banks": {
            "arkipäivät": {"weekly_addition": "3h", "max_balance": "4h"},
        },
        "blackouts": [],
    }
    replacement = {
        "banks": {
            "arkipäivät": {
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "weekly_addition": 3,
                "max_balance": 4,
            },
        },
        "blackouts": [],
    }
    for definition in (original, replacement):
        ctx = FakeContext(
            message_text=(
                "profiili määritä changing "
                + json.dumps(definition, ensure_ascii=False)
            ),
            sender=ADMIN_PHONE,
            sent_messages=[],
        )
        _run_handle(manager, ctx)
        assert "Virheellinen profiili" not in ctx.sent_messages[0]

    in_memory = manager._profiles_by_name["changing"].banks["arkipäivät"]
    persisted = next(profile for profile in store.list_rule_profiles() if profile.name == "changing")

    assert in_memory.is_day_bank is True
    assert in_memory.days == (0, 1, 2, 3, 4)
    assert persisted.banks["arkipäivät"].is_day_bank is True
    assert persisted.banks["arkipäivät"].days == (0, 1, 2, 3, 4)


def test_playtime_manager_profile_database_error_is_reported(tmp_path: Path, monkeypatch) -> None:
    manager, store = _build_manager(tmp_path)
    profile_json = _profile_json()
    monkeypatch.setattr(
        store,
        "upsert_rule_profile",
        lambda _profile: (_ for _ in ()).throw(sqlite3.IntegrityError("test error")),
    )
    ctx = FakeContext(
        message_text=f"profile define strict {profile_json}",
        sender=ADMIN_PHONE,
        sent_messages=[],
    )

    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert "strict" not in manager._profiles_by_name


def test_admin_can_update_a_named_bank(tmp_path: Path) -> None:
    manager, store = _build_manager(
        tmp_path,
        banks={
            "earned": BankProfile("earned", 60, 300),
            "weekly": BankProfile("weekly", 90, 90),
        },
    )
    ctx = FakeContext(message_text="TestChild weekly 1h", sender=ADMIN_PHONE, sent_messages=[])

    _run_handle(manager, ctx)

    assert store.get_bank_balance(CHILD_PHONE, "weekly") == 60
    assert store.get_bank_balance(CHILD_PHONE, "earned") == 0

    default_ctx = FakeContext(message_text="TestChild 1h", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, default_ctx)
    assert store.get_bank_balance(CHILD_PHONE, "earned") == 60
    assert store.get_bank_balance(CHILD_PHONE, "weekly") == 60


def test_fresh_install_requires_profile_definition_and_assignment(tmp_path: Path) -> None:
    manager, _store = _build_manager(tmp_path, default_rule_profile=None)
    status = FakeContext(message_text="status", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, status)
    assert manager._has_profile(CHILD_PHONE) is False
    assert len(status.sent_messages) == 1

    profile_json = _profile_json(
        {
            "earned": {"weekly_addition": "14h", "max_balance": "42h"},
            "weekly": {"weekly_addition": "30h", "max_balance": "30h"},
        }
    )
    _run_handle(
        manager,
        FakeContext(message_text=f"profile define normal {profile_json}", sender=ADMIN_PHONE, sent_messages=[]),
    )
    _run_handle(
        manager,
        FakeContext(message_text="profile use normal TestChild", sender=ADMIN_PHONE, sent_messages=[]),
    )
    assert manager._has_profile(CHILD_PHONE) is True
    profile = manager._profile_for_child(CHILD_PHONE)
    assert list(profile.banks) == ["earned", "weekly"]



def test_playtime_manager_profile_use_default_assigns_default_profile_to_child(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)

    strict_payload = _profile_json()
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


@pytest.mark.parametrize(
    ("language", "header", "total"),
    [
        ("en", "Pending claims for TestChild (2):", "Total: 1h 30m"),
        ("fi", "Odottavat pyynnöt (TestChild, 2 kpl):", "Yhteensä: 1h 30m"),
    ],
)
def test_activity_claim_response_includes_updated_pending_overview(
    tmp_path: Path, language: str, header: str, total: str,
) -> None:
    manager, store = _build_two_child_manager(tmp_path, bot_language=language)
    store.set_bank_balance(CHILD_PHONE, 60)
    submitted_at = datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc)
    store.add_activity_claim(CHILD_PHONE, 60, "1h cleaned room", submitted_at)
    store.add_activity_claim(SECOND_PHONE, 90, "1h30m other child's activity", submitted_at)

    ctx = FakeContext(message_text="30m played guitar", sender=CHILD_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    confirmation, overview = ctx.sent_messages[0].split("\n", 1)
    assert "30m played guitar" in confirmation
    assert header in overview
    assert "1h cleaned room" in overview
    assert "2025-01-06 12:00" in overview
    assert "30m played guitar" in overview
    assert total in overview
    assert "other child's activity" not in overview
    claims = store.get_pending_claims(CHILD_PHONE)
    assert len(claims) == 2
    assert claims[-1].claimed_minutes == 30
    assert claims[-1].description == "30m played guitar"
    assert store.get_bank_balance(CHILD_PHONE) == 60
    assert store.get_active_session(CHILD_PHONE) is None


@pytest.mark.parametrize(
    ("command", "submitted_at", "local_timestamp"),
    [
        (command, datetime(2025, 1, 6, 12, 0, tzinfo=timezone.utc), "2025-01-06 14:00")
        for command in ("30m", "30m played guitar", "status", "claims", "ack")
    ] + [
        ("claims", datetime(2025, 7, 6, 12, 0, tzinfo=timezone.utc), "2025-07-06 15:00"),
        ("claims", datetime(2025, 7, 6, 22, 0, tzinfo=timezone.utc), "2025-07-07 01:00"),
    ],
)
def test_claim_submission_times_use_configured_timezone(
    tmp_path: Path, command: str, submitted_at: datetime, local_timestamp: str,
) -> None:
    manager, store = _build_manager(tmp_path, timezone="Europe/Helsinki")
    store.add_activity_claim(CHILD_PHONE, 60, "1h cleaned room", submitted_at)
    store.set_child_block_mode(CHILD_PHONE, True)
    sender = ADMIN_PHONE if command in {"claims", "ack"} else CHILD_PHONE

    ctx = FakeContext(message_text=command, sender=sender, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert f"1h cleaned room (submitted {local_timestamp})" in ctx.sent_messages[0]


def test_admin_ack_grants_total_and_clears_claims(tmp_path: Path) -> None:
    manager, store = _build_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, 60)
    store.add_activity_claim(CHILD_PHONE, 30, "30m played guitar", datetime.now(timezone.utc))
    store.add_activity_claim(CHILD_PHONE, 60, "1h cleaned room", datetime.now(timezone.utc))

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
    store.add_activity_claim(CHILD_PHONE, 30, "30m played guitar", datetime.now(timezone.utc))

    ctx = FakeContext(message_text="TestChild 1h", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert len(ctx.sent_messages) == 1
    assert store.get_bank_balance(CHILD_PHONE) == 120  # 60 + 60
    assert store.get_pending_claims(CHILD_PHONE) == []


# ---------------------------------------------------------------------------
# Wildcard (*) placeholder tests
# ---------------------------------------------------------------------------

SECOND_PHONE = "+1234567892"


def _build_two_child_manager(tmp_path: Path, **settings_overrides: Any) -> tuple[PlaytimeManager, PlaytimeStore]:
    children = {
        CHILD_PHONE: Child(CHILD_PHONE, "child123", "TestChild"),
        SECOND_PHONE: Child(SECOND_PHONE, "child456", "SecondChild"),
    }
    return _build_manager(tmp_path, children=children, **settings_overrides)


def test_admin_wildcard_child_block_targets_all(tmp_path: Path) -> None:
    manager, store = _build_two_child_manager(tmp_path)

    ctx = FakeContext(message_text="block *", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert store.is_child_block_mode_enabled(CHILD_PHONE) is True
    assert store.is_child_block_mode_enabled(SECOND_PHONE) is True
    assert len(ctx.sent_messages) == 2


def test_admin_wildcard_child_unblock_targets_all(tmp_path: Path) -> None:
    manager, store = _build_two_child_manager(tmp_path)
    store.set_child_block_mode(CHILD_PHONE, True)
    store.set_child_block_mode(SECOND_PHONE, True)

    ctx = FakeContext(message_text="unblock *", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert store.is_child_block_mode_enabled(CHILD_PHONE) is False
    assert store.is_child_block_mode_enabled(SECOND_PHONE) is False
    assert len(ctx.sent_messages) == 2


def test_admin_wildcard_child_bank_command_targets_all_children(tmp_path: Path) -> None:
    manager, store = _build_two_child_manager(tmp_path)
    store.set_bank_balance(CHILD_PHONE, 60)
    store.set_bank_balance(SECOND_PHONE, 30)

    ctx = FakeContext(message_text="* +1h", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert store.get_bank_balance(CHILD_PHONE) == 120
    assert store.get_bank_balance(SECOND_PHONE) == 90
    assert len(ctx.sent_messages) == 2


def test_admin_wildcard_bank_modifies_all_profile_banks_for_named_child(tmp_path: Path) -> None:
    manager, store = _build_manager(
        tmp_path,
        banks={
            "earned": BankProfile("earned", 60, 300),
            "weekly": BankProfile("weekly", 90, 90),
        },
    )
    store.set_bank_balance(CHILD_PHONE, 30, "earned")
    store.set_bank_balance(CHILD_PHONE, 20, "weekly")

    ctx = FakeContext(message_text="TestChild * +30m", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert store.get_bank_balance(CHILD_PHONE, "earned") == 60
    assert store.get_bank_balance(CHILD_PHONE, "weekly") == 50
    assert len(ctx.sent_messages) == 1


def test_admin_wildcard_both_modifies_all_profile_banks_for_all_children(tmp_path: Path) -> None:
    manager, store = _build_two_child_manager(
        tmp_path,
        banks={
            "earned": BankProfile("earned", 60, 300),
            "weekly": BankProfile("weekly", 90, 90),
        },
    )
    store.set_bank_balance(CHILD_PHONE, 30, "earned")
    store.set_bank_balance(CHILD_PHONE, 20, "weekly")
    store.set_bank_balance(SECOND_PHONE, 10, "earned")
    store.set_bank_balance(SECOND_PHONE, 5, "weekly")

    ctx = FakeContext(message_text="* * +30m", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert store.get_bank_balance(CHILD_PHONE, "earned") == 60
    assert store.get_bank_balance(CHILD_PHONE, "weekly") == 50
    assert store.get_bank_balance(SECOND_PHONE, "earned") == 40
    assert store.get_bank_balance(SECOND_PHONE, "weekly") == 35
    assert len(ctx.sent_messages) == 2


def test_admin_wildcard_bank_only_touches_active_profile_banks(tmp_path: Path) -> None:
    """* for bank name must not affect banks outside the child's active profile."""
    manager, store = _build_manager(
        tmp_path,
        banks={
            "earned": BankProfile("earned", 60, 300),
        },
    )
    store.set_bank_balance(CHILD_PHONE, 30, "earned")
    # Write a balance for a bank not in the active profile to confirm it is untouched.
    store.set_bank_balance(CHILD_PHONE, 50, "legacy")

    ctx = FakeContext(message_text="TestChild * +30m", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert store.get_bank_balance(CHILD_PHONE, "earned") == 60
    assert store.get_bank_balance(CHILD_PHONE, "legacy") == 50  # unchanged


def test_admin_profile_use_wildcard_assigns_to_all_children(tmp_path: Path) -> None:
    manager, _store = _build_two_child_manager(tmp_path)
    profile_json = _profile_json()
    _run_handle(
        manager,
        FakeContext(message_text=f"profile define strict {profile_json}", sender=ADMIN_PHONE, sent_messages=[]),
    )

    ctx = FakeContext(message_text="profile use strict *", sender=ADMIN_PHONE, sent_messages=[])
    _run_handle(manager, ctx)

    assert manager._profile_for_child(CHILD_PHONE).name == "strict"
    assert manager._profile_for_child(SECOND_PHONE).name == "strict"
    assert len(ctx.sent_messages) == 2
