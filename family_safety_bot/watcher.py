from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
from datetime import datetime, timezone
from typing import Awaitable, Callable

import httpx
from signalbot import Command, Context

from family_safety_bot.config import (
    Child,
    RuleProfile,
    Settings,
    WEEKDAY_NAME_TO_INDEX,
    WEEKDAY_SUFFIXES,
    normalize_profile_name,
    parse_day_blackout_periods,
)
from family_safety_bot.durations import parse_activity_claim, parse_duration_minutes, parse_signed_duration_minutes
from family_safety_bot.formatting import format_duration
from family_safety_bot.i18n import I18n
from family_safety_bot.ms_family import MicrosoftFamilyApi
from family_safety_bot.rules import PlaytimeRules
from family_safety_bot.storage import ActivityClaim, PlaytimeStore

logger = logging.getLogger(__name__)
NUMERIC_ONLY_RE = re.compile(r"^\d+(?:\.\d+)?$")
_COMMAND_KEYS = (
    "status",
    "help",
    "end",
    "admin_test",
    "admin_end",
    "admin_setbreak",
    "admin_rollover",
    "admin_block",
    "admin_unblock",
    "admin_claims",
    "admin_ack",
    "admin_profile",
    "admin_profile_list",
    "admin_profile_show",
    "admin_profile_define",
    "admin_profile_use",
)
_PROFILE_REQUIRED_ENV_KEYS = (
    "WEEKLY_ADDITION_TIME",
    "MAX_BANK_TIME",
    "BREAK_BALANCE_MAX_TIME",
    "BREAK_RECOVERY_RATE",
    "BLACKOUT_PERIOD_MON",
    "BLACKOUT_PERIOD_TUE",
    "BLACKOUT_PERIOD_WED",
    "BLACKOUT_PERIOD_THU",
    "BLACKOUT_PERIOD_FRI",
    "BLACKOUT_PERIOD_SAT",
    "BLACKOUT_PERIOD_SUN",
)


class PlaytimeManager(Command):
    """Handle playtime requests via Signal commands."""
    
    def __init__(self, settings: Settings, store: PlaytimeStore) -> None:
        super().__init__()
        self._settings = settings
        self._store = store
        self._i18n = I18n(os.environ.get("BOT_LANGUAGE", "en"))
        self._commands = {
            key: set(self._i18n.command_aliases(key))
            for key in _COMMAND_KEYS
        }
        self._ms_api: MicrosoftFamilyApi | None = None
        self._ms_api_lock = asyncio.Lock()
        self._default_profile = settings.default_rule_profile
        self._profiles_by_name: dict[str, RuleProfile] = {}
        self._active_profile_name_by_child: dict[str, str] = {}
        self._initialize_profiles()

        self._rules_by_child = {
            phone_number: PlaytimeRules(
                child_id=phone_number,
                settings=settings,
                store=store,
                profile_provider=lambda phone_number=phone_number: self._profile_for_child(phone_number),
                i18n=self._i18n,
            )
            for phone_number in settings.children
        }
        self._children_by_name: dict[str, tuple[Child, PlaytimeRules]] = {}
        for phone_number, child in settings.children.items():
            key = child.name.lower()
            if key in self._children_by_name:
                existing_child, _existing_rules = self._children_by_name[key]
                raise ValueError(
                    "Duplicate child name detected (case-insensitive): "
                    f"{existing_child.name!r} and {child.name!r}"
                )
            self._children_by_name[key] = (child, self._rules_by_child[phone_number])

    def _initialize_profiles(self) -> None:
        existing = self._store.list_rule_profiles()
        if not existing:
            self._store.upsert_rule_profile(self._default_profile)
            existing = [self._default_profile]
        self._profiles_by_name = {profile.name.lower(): profile for profile in existing}
        if self._default_profile.name.lower() not in self._profiles_by_name:
            self._store.upsert_rule_profile(self._default_profile)
            self._profiles_by_name[self._default_profile.name.lower()] = self._default_profile

        self._active_profile_name_by_child = {}
        for child_id in self._settings.children:
            child_active_name = self._store.get_active_rule_profile_name_for_child(child_id)
            if child_active_name and child_active_name.lower() in self._profiles_by_name:
                self._active_profile_name_by_child[child_id] = self._profiles_by_name[child_active_name.lower()].name

    def _profile_for_child(self, phone_number: str) -> RuleProfile:
        active_name = self._active_profile_name_by_child.get(phone_number, self._default_profile.name)
        return self._profiles_by_name[active_name.lower()]
    
    async def _resolve_child(
        self,
        ctx: Context,
        child_name: str,
    ) -> tuple[Child, PlaytimeRules] | None:
        found = self._children_by_name.get(child_name.lower())
        if found:
            return found
        names = ", ".join(c.name for c in self._settings.children.values())
        await ctx.send(
            self._i18n.msg(
                "watcher.child_not_found",
                child_name=child_name,
                names=names,
            )
        )
        return None

    def _all_children(self) -> list[tuple[Child, PlaytimeRules]]:
        return [
            (child, self._rules_by_child[phone_number])
            for phone_number, child in self._settings.children.items()
        ]

    async def _parse_children_and_minutes(
        self,
        ctx: Context,
        payload: str,
        parse_minutes: Callable[[str], int | None],
    ) -> tuple[list[tuple[Child, PlaytimeRules]], int, bool] | None:
        stripped_payload = payload.strip()
        if not stripped_payload:
            return None

        parts = stripped_payload.split(maxsplit=1)
        if len(parts) == 2:
            child_name, duration_text = parts
            minutes = parse_minutes(duration_text)
            if minutes is not None:
                resolved = await self._resolve_child(ctx, child_name)
                if not resolved:
                    return None
                child, child_rules = resolved
                is_relative = not duration_text.lstrip().startswith("=")
                return ([(child, child_rules)], minutes, is_relative)

        minutes = parse_minutes(stripped_payload)
        if minutes is None:
            return None
        is_relative = not stripped_payload.lstrip().startswith("=")
        return (self._all_children(), minutes, is_relative)

    async def _parse_children_payload(
        self,
        ctx: Context,
        payload: str,
    ) -> list[tuple[Child, PlaytimeRules]] | None:
        child_name = payload.strip()
        if not child_name:
            return self._all_children()
        resolved = await self._resolve_child(ctx, child_name)
        if not resolved:
            return None
        child, child_rules = resolved
        return [(child, child_rules)]

    @staticmethod
    def _profile_summary(profile: RuleProfile) -> str:
        day_to_ranges: dict[int, list[str]] = {idx: [] for idx in range(7)}
        for weekday, start_time, end_time in profile.blackout_periods:
            day_to_ranges.setdefault(weekday, []).append(f"{start_time}-{end_time}")
        blackout_lines = []
        for idx, suffix in enumerate(WEEKDAY_SUFFIXES):
            ranges = ",".join(day_to_ranges.get(idx, [])) or "none"
            blackout_lines.append(f"  {suffix}: {ranges}")
        return (
            f"Profile: {profile.name}\n"
            f"  weekly_addition: {format_duration(profile.weekly_addition_minutes)}\n"
            f"  max_bank: {format_duration(profile.max_bank_minutes)}\n"
            f"  break_max: {format_duration(profile.break_balance_max_minutes)}\n"
            f"  break_recovery_rate: {profile.break_recovery_rate:.2f}\n"
            f"  blackouts:\n"
            + "\n".join(blackout_lines)
        )

    @staticmethod
    def _parse_env_assignments(payload: str) -> dict[str, str] | None:
        try:
            tokens = shlex.split(payload)
        except ValueError:
            return None
        assignments: dict[str, str] = {}
        for token in tokens:
            if "=" not in token:
                return None
            key, value = token.split("=", 1)
            key = key.strip()
            if not key:
                return None
            assignments[key] = value.strip()
        return assignments

    @staticmethod
    def _required_profile_keys_message() -> str:
        return "Required env-style keys: " + ", ".join(_PROFILE_REQUIRED_ENV_KEYS)

    @staticmethod
    def _profile_usage() -> str:
        return "❌ Usage: profile list|show <name>|define <name> KEY=VALUE ...|use <profile> <child>"

    def _build_profile_from_env_assignments(self, profile_name: str, assignments: dict[str, str]) -> RuleProfile:
        missing = [key for key in _PROFILE_REQUIRED_ENV_KEYS if key not in assignments]
        if missing:
            raise ValueError(f"Missing keys: {', '.join(missing)}")

        weekly_addition_minutes = parse_duration_minutes(assignments["WEEKLY_ADDITION_TIME"])
        max_bank_minutes = parse_duration_minutes(assignments["MAX_BANK_TIME"])
        break_balance_max_minutes = parse_duration_minutes(assignments["BREAK_BALANCE_MAX_TIME"])
        if weekly_addition_minutes is None or weekly_addition_minutes <= 0:
            raise ValueError("WEEKLY_ADDITION_TIME must be a positive duration like 14h.")
        if max_bank_minutes is None or max_bank_minutes <= 0:
            raise ValueError("MAX_BANK_TIME must be a positive duration like 42h.")
        if break_balance_max_minutes is None or break_balance_max_minutes <= 0:
            raise ValueError("BREAK_BALANCE_MAX_TIME must be a positive duration like 3h.")
        try:
            break_recovery_rate = float(assignments["BREAK_RECOVERY_RATE"])
        except ValueError as exc:
            raise ValueError("BREAK_RECOVERY_RATE must be a positive number like 3.0.") from exc
        if break_recovery_rate <= 0:
            raise ValueError("BREAK_RECOVERY_RATE must be > 0.")

        blackout_periods: list[tuple[int, str, str]] = []
        for suffix in WEEKDAY_SUFFIXES:
            weekday_key = f"BLACKOUT_PERIOD_{suffix}"
            day_value = assignments[weekday_key]
            if not day_value:
                continue
            weekday = WEEKDAY_NAME_TO_INDEX[suffix.lower()]
            blackout_periods.extend(
                parse_day_blackout_periods(
                    day_value,
                    weekday,
                    source=f"profile env {weekday_key}",
                )
            )

        return RuleProfile(
            name=profile_name,
            weekly_addition_minutes=weekly_addition_minutes,
            max_bank_minutes=max_bank_minutes,
            break_balance_max_minutes=break_balance_max_minutes,
            break_recovery_rate=break_recovery_rate,
            blackout_periods=blackout_periods,
        )

    async def _handle_profile_command(self, ctx: Context, payload: str) -> None:
        payload = payload.strip()
        if not payload:
            await ctx.send(self._profile_usage())
            return

        parts = payload.split()
        subcommand = parts[0].lower()

        if subcommand in self._commands["admin_profile_list"]:
            ordered = sorted(self._profiles_by_name.values(), key=lambda p: p.name.lower())
            lines = [
                f"* {profile.name}" + (" (default)" if profile.name.lower() == self._default_profile.name.lower() else "")
                for profile in ordered
            ]
            await ctx.send("Profiles:\n" + "\n".join(lines))
            return

        if subcommand in self._commands["admin_profile_show"]:
            if len(parts) != 2:
                await ctx.send("❌ Usage: profile show <name>")
                return
            profile = self._profiles_by_name.get(parts[1].lower())
            if profile is None:
                await ctx.send(f"❌ Unknown profile: {parts[1]}")
                return
            await ctx.send(self._profile_summary(profile))
            return

        if subcommand in self._commands["admin_profile_use"]:
            if len(parts) != 3:
                await ctx.send("❌ Usage: profile use <profile> <child>")
                return
            profile = self._profiles_by_name.get(parts[1].lower())
            if profile is None:
                await ctx.send(f"❌ Unknown profile: {parts[1]}")
                return
            resolved = await self._resolve_child(ctx, parts[2])
            if not resolved:
                return
            child, _child_rules = resolved
            self._active_profile_name_by_child[child.phone_number] = profile.name
            self._store.set_active_rule_profile_name_for_child(child.phone_number, profile.name)
            await ctx.send(f"✅ Active profile for {child.name} switched to '{profile.name}'.")
            return

        if subcommand in self._commands["admin_profile_define"]:
            parts = payload.split(maxsplit=2)
            if len(parts) != 3:
                await ctx.send(
                    "❌ Usage: profile define <name> KEY=VALUE ...\n"
                    + self._required_profile_keys_message()
                )
                return
            try:
                name = normalize_profile_name(parts[1])
            except ValueError as exc:
                await ctx.send(f"❌ {exc}")
                return
            assignments = self._parse_env_assignments(parts[2])
            if assignments is None:
                await ctx.send(
                    "❌ Invalid env assignment format. Use KEY=VALUE pairs.\n"
                    + self._required_profile_keys_message()
                )
                return
            try:
                created = self._build_profile_from_env_assignments(name, assignments)
            except ValueError as exc:
                await ctx.send(f"❌ {exc}\n{self._required_profile_keys_message()}")
                return
            self._store.upsert_rule_profile(created)
            self._profiles_by_name[name.lower()] = created
            await ctx.send(f"✅ Defined profile '{name}'.\n{self._profile_summary(created)}")
            return

        await ctx.send(self._profile_usage())

    async def _handle_admin_command(self, ctx: Context, message_text: str, message_lower: str) -> bool:
        parts_lower = message_lower.split(maxsplit=1)
        if not parts_lower:
            return False
        command = parts_lower[0]
        payload = message_text.split(maxsplit=1)[1] if len(parts_lower) == 2 else ""

        if command in self._commands["admin_block"]:
            resolved = await self._parse_children_payload(ctx, payload)
            if not resolved:
                return True
            for block_child, block_rules in resolved:
                await self.admin_block_child_command(ctx, block_child, block_rules)
            return True
        if command in self._commands["admin_unblock"]:
            resolved = await self._parse_children_payload(ctx, payload)
            if not resolved:
                return True
            for unblock_child, _unblock_rules in resolved:
                self._store.set_child_block_mode(unblock_child.phone_number, False)
                await ctx.send(self._i18n.msg("watcher.admin_child_unblocked", child_name=unblock_child.name))
            return True

        if command in self._commands["admin_test"]:
            resolved = await self._parse_children_and_minutes(ctx, payload, parse_duration_minutes)
            if not resolved:
                return True
            test_children, test_minutes, _is_relative = resolved
            for test_child, test_rules in test_children:
                await self.request_command(
                    ctx,
                    test_child,
                    test_rules,
                    test_minutes,
                    test_child.name + " (TEST)",
                )
            return True

        if command in self._commands["admin_end"]:
            resolved = await self._parse_children_payload(ctx, payload)
            if not resolved:
                return True
            for end_child, end_rules in resolved:
                await self.admin_end_session_command(ctx, end_child, end_rules)
            return True

        if command in self._commands["admin_setbreak"]:
            resolved = await self._parse_children_and_minutes(ctx, payload, parse_duration_minutes)
            if not resolved:
                return True
            break_children, break_minutes, _is_relative = resolved
            for break_child, break_rules in break_children:
                await ctx.send(
                    self._i18n.msg(
                        "watcher.admin_prefix",
                        child_name=break_child.name,
                        message=break_rules.set_consumed_break_debt(break_minutes),
                    )
                )
            return True

        if command in self._commands["admin_rollover"]:
            resolved = await self._parse_children_payload(ctx, payload)
            if not resolved:
                return True
            for rollover_child, rollover_rules in resolved:
                await ctx.send(
                    self._i18n.msg(
                        "watcher.admin_prefix",
                        child_name=rollover_child.name,
                        message=rollover_rules.rollover_week(),
                    )
                )
            return True

        if command in self._commands["admin_profile"]:
            await self._handle_profile_command(ctx, payload)
            return True

        if command in self._commands["admin_claims"]:
            resolved = await self._parse_children_payload(ctx, payload)
            if not resolved:
                return True
            for claims_child, _ in resolved:
                claims = self._store.get_pending_claims(claims_child.phone_number)
                if not claims:
                    await ctx.send(self._i18n.msg("watcher.no_pending_claims", child_name=claims_child.name))
                else:
                    header = self._i18n.msg(
                        "watcher.claims_header",
                        child_name=claims_child.name,
                        count=len(claims),
                    )
                    await ctx.send(header + "\n" + self._format_claims_block(claims))
            return True

        if command in self._commands["admin_ack"]:
            resolved = await self._parse_children_payload(ctx, payload)
            if not resolved:
                return True
            for ack_child, ack_rules in resolved:
                claims = self._store.get_pending_claims(ack_child.phone_number)
                if not claims:
                    await ctx.send(self._i18n.msg("watcher.no_pending_claims", child_name=ack_child.name))
                    continue
                total_minutes = sum(c.claimed_minutes for c in claims)
                self._store.mark_all_claims_handled(ack_child.phone_number, total_minutes)
                bank_result = ack_rules.modify_bank(total_minutes)
                ack_header = self._i18n.msg(
                    "watcher.ack_header",
                    count=len(claims),
                    total=format_duration(total_minutes),
                )
                full_message = ack_header + "\n" + self._format_claims_block(claims) + "\n" + bank_result
                await ctx.send(
                    self._i18n.msg("watcher.admin_prefix", child_name=ack_child.name, message=full_message)
                )
            return True

        resolved = await self._parse_children_and_minutes(ctx, message_text, parse_signed_duration_minutes)
        if not resolved:
            return False
        bank_children, bank_minutes, is_relative = resolved
        for bank_child, bank_rules in bank_children:
            pending_note = self._auto_handle_pending_claims(bank_child)
            result = bank_rules.modify_bank(bank_minutes) if is_relative else bank_rules.set_bank(bank_minutes)
            await ctx.send(
                self._i18n.msg(
                    "watcher.admin_prefix",
                    child_name=bank_child.name,
                    message=result + pending_note,
                )
            )
        return True

    def setup(self) -> None:
        """Set up scheduled tasks."""
        # Weekly rollover check - run Monday at 00:01
        self.bot.scheduler.add_job(  # type: ignore[union-attr]
            self._check_weekly_rollover,
            trigger="cron",
            day_of_week="mon",
            hour=0,
            minute=1,
            timezone=self._settings.timezone,
            misfire_grace_time=None,
            coalesce=False,
            max_instances=1
        )
        logger.info("Scheduled weekly rollover check for Monday 00:01")

    async def handle(self, ctx: Context) -> None:
        """Handle incoming messages from the group."""
        raw_text = ctx.message.text
        if raw_text is None:
            return
        message_text = raw_text.strip()
        if not message_text:
            return
        message_lower = message_text.lower()
        sender = ctx.message.source
        
        # Determine if sender is child or admin
        is_admin = sender in self._settings.signal_admins
        child = self._settings.children.get(sender)
        is_child = child is not None
        
        if not (is_admin or is_child):
            # Unknown sender in group - ignore silently
            logger.info("Ignoring message from unknown sender in group: %s", sender)
            return
        
        # Get sender name for context
        sender_name = child.name if is_child else "Admin"
        
        # Get rules instance for this child (if child), or None (if admin)
        rules = self._rules_by_child.get(sender) if is_child else None
        
        if message_lower in self._commands["help"]:
            await ctx.send(self._i18n.msg("watcher.help_admin" if is_admin else "watcher.help_child"))
            return

        if message_lower in self._commands["status"]:
            child_sections: list[str] = []
            for phone, child_obj in self._settings.children.items():
                child_status = self._rules_by_child[phone].get_status()
                if self._store.is_child_block_mode_enabled(phone):
                    child_sections.append(
                        f"[{child_obj.name}]\n"
                        + self._i18n.msg("watcher.status_child_block_mode_enabled")
                        + "\n"
                        + child_status
                    )
                else:
                    child_sections.append(f"[{child_obj.name}]\n{child_status}")
            await ctx.send("\n\n".join(child_sections))
            return

        parsed_minutes = parse_duration_minutes(message_text)
        if parsed_minutes is not None and rules is not None:
            await self.request_command(ctx, child, rules, parsed_minutes, sender_name)
            return

        if is_child and rules is not None:
            claim = parse_activity_claim(message_text)
            if claim is not None:
                claim_minutes, description = claim
                await self.claim_command(ctx, child, claim_minutes, description, sender_name)
                return

        if is_child and NUMERIC_ONLY_RE.match(message_text):
            await ctx.send(self._i18n.msg("watcher.numeric_unit_required", sender_name=sender_name))
            return

        if message_lower in self._commands["end"] and rules is not None:
            await self.end_session_command(ctx, child, rules, sender_name)
            return

        if is_admin and await self._handle_admin_command(ctx, message_text, message_lower):
            return
        
        # Unknown command
        await ctx.send(self._i18n.msg("watcher.help_admin" if is_admin else "watcher.help_child"))

    async def _call_ms_api_with_retry(
        self,
        action_name: str,
        action: Callable[[MicrosoftFamilyApi], Awaitable[bool]],
    ) -> bool:
        async with self._ms_api_lock:
            if self._ms_api is None:
                self._ms_api = MicrosoftFamilyApi(
                    self._settings.ms_family_email,
                    self._settings.ms_family_password,
                )

            api = self._ms_api
            await api.ensure_authenticated()
            success = await action(api)
            if success:
                return True
            if api.last_status_code != 401:
                return False

            logger.info(
                "Microsoft %s API returned 401; retrying once with forced re-authentication.",
                action_name,
            )
            await api.ensure_authenticated(force=True)
            return await action(api)

    async def _grant_via_ms_api(self, child_id: str, minutes: int) -> bool:
        return await self._call_ms_api_with_retry(
            "grant",
            lambda api: api.grant_screen_time(child_id, minutes),
        )

    async def _block_via_ms_api(self, child_id: str) -> bool:
        return await self._call_ms_api_with_retry(
            "block",
            lambda api: api.block_screen_time(child_id),
        )

    async def request_command(
        self,
        ctx: Context,
        child: Child,
        rules: PlaytimeRules,
        minutes: int,
        sender_name: str,
    ) -> None:
        """Handle a playtime request from child.
        
        AUTOMATIC GRANTING: If the request satisfies all rules, time is granted
        immediately without manual approval. The Microsoft Family Safety API
        is called automatically (if configured and available).
        """
        if minutes <= 0:
            await ctx.send(self._i18n.msg("watcher.positive_duration_required", sender_name=sender_name))
            return

        if self._store.is_child_block_mode_enabled(child.phone_number):
            await ctx.send(
                self._i18n.msg(
                    "watcher.request_denied",
                    sender_name=sender_name,
                    reason=self._i18n.msg("watcher.request_blocked_by_admin"),
                )
            )
            return
        
        # Evaluate request against all rules
        decision = rules.evaluate_request(minutes)
        
        if not decision.allowed:
            await ctx.send(self._i18n.msg("watcher.request_denied", sender_name=sender_name, reason=decision.reason))
            return
        
        # For early session ends, `end_session_command` / `admin_end_session_command`
        # first apply an immediate Microsoft block before local completion.
        try:
            grant_success = await self._grant_via_ms_api(child.ms_account_id, decision.minutes_granted)
        except (ValueError, RuntimeError, httpx.HTTPError):
            logger.exception("Failed to grant time via Microsoft API")
            await ctx.send(self._i18n.msg("watcher.request_grant_error", sender_name=sender_name))
            return
        if not grant_success:
            await ctx.send(self._i18n.msg("watcher.request_grant_failed", sender_name=sender_name))
            return

        # Only commit local tracking after successful upstream grant.
        _, message = rules.grant_playtime(decision.minutes_granted)

        response_lines = [f"[{sender_name}]"]
        if decision.reason:
            response_lines.append(decision.reason)
        response_lines.append(message)

        # Send to group with child name prefix
        await ctx.send("\n".join(response_lines))

    async def admin_block_child_command(self, ctx: Context, child: Child, rules: PlaytimeRules) -> None:
        """Enable grant block mode for a child and end any active session."""
        self._store.set_child_block_mode(child.phone_number, True)
        block_msg = self._i18n.msg("watcher.admin_child_blocked", child_name=child.name)
        if not rules.has_active_session():
            await ctx.send(block_msg)
            return
        try:
            block_success = await self._block_via_ms_api(child.ms_account_id)
        except (ValueError, RuntimeError, httpx.HTTPError):
            logger.exception("Failed to block time via Microsoft API")
            await ctx.send(
                block_msg + "\n"
                + self._i18n.msg("watcher.admin_end_block_error", child_name=child.name)
            )
            return
        if not block_success:
            await ctx.send(
                block_msg + "\n"
                + self._i18n.msg("watcher.admin_end_block_rejected", child_name=child.name)
            )
            return
        result = rules.complete_session()
        await ctx.send(
            block_msg + "\n"
            + self._i18n.msg("watcher.admin_end_session", child_name=child.name, result=result)
        )

    async def admin_end_session_command(self, ctx: Context, child: Child, rules: PlaytimeRules) -> None:
        """Admin end active session."""
        if not rules.has_active_session():
            result = rules.complete_session()
            await ctx.send(self._i18n.msg("watcher.admin_end_session", child_name=child.name, result=result))
            return

        try:
            block_success = await self._block_via_ms_api(child.ms_account_id)
        except (ValueError, RuntimeError, httpx.HTTPError):
            logger.exception("Failed to block time via Microsoft API")
            await ctx.send(self._i18n.msg("watcher.admin_end_block_error", child_name=child.name))
            return
        if not block_success:
            await ctx.send(self._i18n.msg("watcher.admin_end_block_rejected", child_name=child.name))
            return

        result = rules.complete_session()
        await ctx.send(self._i18n.msg("watcher.admin_end_session", child_name=child.name, result=result))

    async def end_session_command(
        self,
        ctx: Context,
        child: Child,
        rules: PlaytimeRules,
        sender_name: str,
    ) -> None:
        if not rules.has_active_session():
            result = rules.complete_session()
            await ctx.send(f"[{sender_name}]\n{result}")
            return

        try:
            block_success = await self._block_via_ms_api(child.ms_account_id)
        except (ValueError, RuntimeError, httpx.HTTPError):
            logger.exception("Failed to block time via Microsoft API")
            await ctx.send(self._i18n.msg("watcher.end_block_error", sender_name=sender_name))
            return
        if not block_success:
            await ctx.send(self._i18n.msg("watcher.end_block_failed", sender_name=sender_name))
            return

        result = rules.complete_session()
        await ctx.send(f"[{sender_name}]\n{result}")

    async def _check_weekly_rollover(self) -> None:
        """Scheduled task to handle weekly rollover for all children."""
        results = [
            f"{child.name}: {self._rules_by_child[phone].rollover_week()}"
            for phone, child in self._settings.children.items()
        ]
        for result in results:
            logger.info("Weekly rollover for %s", result)
        
        # Send to group
        try:
            combined_message = self._i18n.msg("watcher.weekly_rollover_header") + "\n\n" + "\n\n".join(results)
            await self.bot.send(self._settings.signal_group_id, combined_message)  # type: ignore[union-attr]
        except Exception:
            logger.exception("Failed to send rollover notification to group")

    async def claim_command(
        self,
        ctx: Context,
        child: Child,
        minutes: int,
        description: str,
        sender_name: str,
    ) -> None:
        """Record an activity claim from a child."""
        self._store.add_activity_claim(
            child.phone_number, minutes, description, datetime.now(timezone.utc)
        )
        await ctx.send(
            self._i18n.msg(
                "watcher.claim_received",
                sender_name=sender_name,
                minutes=format_duration(minutes),
                description=description,
            )
        )

    def _format_claims_block(self, claims: list[ActivityClaim]) -> str:
        """Format claim entries + total as a multi-line string (no header)."""
        lines = []
        for c in claims:
            submitted = c.submitted_at.strftime("%Y-%m-%d %H:%M")
            lines.append(
                self._i18n.msg(
                    "watcher.claims_entry",
                    minutes=format_duration(c.claimed_minutes),
                    description=c.description,
                    submitted_at=submitted,
                )
            )
        total = sum(c.claimed_minutes for c in claims)
        lines.append(self._i18n.msg("watcher.claims_total", total=format_duration(total)))
        return "\n".join(lines)

    def _auto_handle_pending_claims(self, child: Child) -> str:
        """Mark pending claims as handled; return a formatted note (or empty string)."""
        claims = self._store.get_pending_claims(child.phone_number)
        if not claims:
            return ""
        self._store.mark_all_claims_handled(child.phone_number)
        header = self._i18n.msg("watcher.claims_auto_handled_header", count=len(claims))
        return "\n" + header + "\n" + self._format_claims_block(claims)
