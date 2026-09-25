from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import httpx
from dateutil import tz
from signalbot import DataMessageContext, DataMessageHandler, SendMessage, SignalBot

from family_safety_bot.config import (
    Child,
    RuleProfile,
    Settings,
    WEEKDAY_SUFFIXES,
    parse_rule_profile_definition,
)
from family_safety_bot.durations import parse_activity_claim, parse_duration_minutes, parse_signed_duration_minutes
from family_safety_bot.formatting import format_duration
from family_safety_bot.i18n import I18n
from family_safety_bot.ms_family import MicrosoftFamilyApi
from family_safety_bot.rules import PlaytimeRules
from family_safety_bot.storage import ActivityClaim, PlaytimeStore

logger = logging.getLogger(__name__)
NUMERIC_ONLY_RE = re.compile(r"^\d+(?:\.\d+)?$")
HAS_DIGIT_RE = re.compile(r"\d")
TEXT_MENTION_RE = re.compile(r"(^|\s)@\S+")
DAY_BANK_AMOUNT_RE = re.compile(r"^([+\-=]?)(\d+)\s*d(?:ays?)?$", re.IGNORECASE)
IMPLICIT_HELP_MAX_CHARS = 80
_COMMAND_KEYS = (
    "status",
    "help",
    "end",
    "admin_test",
    "admin_end",
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


class PlaytimeManager(DataMessageHandler):
    """Handle playtime requests via Signal commands."""
    
    def __init__(self, settings: Settings, store: PlaytimeStore) -> None:
        self._settings = settings
        self._store = store
        self._tz = tz.gettz(settings.timezone)
        self._i18n = I18n(settings.bot_language)
        self._commands = {
            key: set(self._i18n.command_aliases(key))
            for key in _COMMAND_KEYS
        }
        admin_handlers_map = {
            "admin_block": self._handle_admin_block_command,
            "admin_unblock": self._handle_admin_unblock_command,
            "admin_test": self._handle_admin_test_command,
            "admin_end": self._handle_admin_end_command,
            "admin_rollover": self._handle_admin_rollover_command,
            "admin_profile": self._handle_admin_profile_command,
            "admin_claims": self._handle_admin_claims_command,
            "admin_ack": self._handle_admin_ack_command,
        }
        self._admin_handlers = {
            alias.lower(): handler
            for key, handler in admin_handlers_map.items()
            for alias in self._i18n.command_aliases(key)
        }
        self._ms_api: MicrosoftFamilyApi | None = None
        self._ms_api_lock = asyncio.Lock()
        self._profiles_by_name: dict[str, RuleProfile] = {}
        self._active_profile_name_by_child: dict[str, str] = {}
        self.bot: SignalBot | None = None
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
        self._profiles_by_name = {profile.name.lower(): profile for profile in existing}

        self._active_profile_name_by_child = {}
        for child_id in self._settings.children:
            child_active_name = self._store.get_active_rule_profile_name_for_child(child_id)
            if child_active_name and child_active_name.lower() in self._profiles_by_name:
                self._active_profile_name_by_child[child_id] = self._profiles_by_name[child_active_name.lower()].name

    def _profile_for_child(self, phone_number: str) -> RuleProfile:
        active_name = self._active_profile_name_by_child.get(phone_number)
        if active_name is None:
            raise RuntimeError(f"No rule profile assigned to {phone_number}")
        return self._profiles_by_name[active_name.lower()]

    def _has_profile(self, phone_number: str) -> bool:
        return phone_number in self._active_profile_name_by_child
    
    async def _resolve_child(
        self,
        ctx: DataMessageContext,
        child_name: str,
    ) -> tuple[Child, PlaytimeRules] | None:
        found = self._children_by_name.get(child_name.lower())
        if found:
            return found
        names = ", ".join(c.name for c in self._settings.children.values())
        await ctx.send(
            SendMessage(text=self._i18n.msg(
                "watcher.child_not_found",
                child_name=child_name,
                names=names,
            ))
        )
        return None

    def _all_children(self) -> list[tuple[Child, PlaytimeRules]]:
        return [
            (child, self._rules_by_child[phone_number])
            for phone_number, child in self._settings.children.items()
        ]

    async def _parse_children_and_minutes(
        self,
        ctx: DataMessageContext,
        payload: str,
        parse_minutes: Callable[[str], int | None],
    ) -> tuple[list[tuple[Child, PlaytimeRules]], int, bool] | None:
        if not (stripped := payload.strip()):
            return None

        parts = stripped.split(maxsplit=1)
        if len(parts) == 2:
            child_name, duration_text = parts
            if child_name == "*":
                if (minutes := parse_minutes(duration_text)) is not None:
                    return (self._all_children(), minutes, not duration_text.lstrip().startswith("="))
            elif (minutes := parse_minutes(duration_text)) is not None:
                if resolved := await self._resolve_child(ctx, child_name):
                    return ([(resolved[0], resolved[1])], minutes, not duration_text.lstrip().startswith("="))

        if (minutes := parse_minutes(stripped)) is None:
            return None

        return (self._all_children(), minutes, not stripped.lstrip().startswith("="))

    async def _parse_children_payload(
        self,
        ctx: DataMessageContext,
        payload: str,
    ) -> list[tuple[Child, PlaytimeRules]] | None:
        child_name = payload.strip()
        if not child_name or child_name == "*":
            return self._all_children()
        resolved = await self._resolve_child(ctx, child_name)
        if not resolved:
            return None
        child, child_rules = resolved
        return [(child, child_rules)]

    def _profile_summary(self, profile: RuleProfile) -> str:
        day_to_ranges: dict[int, list[str]] = {idx: [] for idx in range(7)}
        for weekday, start_time, end_time in profile.blackout_periods:
            day_to_ranges[weekday].append(f"{start_time}-{end_time}")
        blackout_lines = "\n".join(
            self._i18n.msg(
                "watcher.profile_summary_blackout_day",
                day=suffix,
                periods=",".join(day_to_ranges[idx]) or self._i18n.msg("watcher.profile_summary_no_blackouts"),
            )
            for idx, suffix in enumerate(WEEKDAY_SUFFIXES)
        )
        bank_lines = "\n".join(
            self._i18n.msg(
                (
                    "watcher.profile_summary_day_bank"
                    if bank.is_day_bank
                    else "watcher.profile_summary_recovery_bank"
                    if bank.is_recovery
                    else "watcher.profile_summary_bank"
                ),
                bank_name=bank.name,
                weekly_addition=(
                    str(bank.weekly_addition_minutes)
                    if bank.is_day_bank
                    else format_duration(bank.weekly_addition_minutes)
                    if bank.weekly_addition_minutes is not None
                    else ""
                ),
                max_balance=(
                    str(bank.max_balance_minutes)
                    if bank.is_day_bank
                    else format_duration(bank.max_balance_minutes)
                ),
                recovery_rate=f"{bank.recovery_rate:g}" if bank.recovery_rate is not None else "",
                days=", ".join(WEEKDAY_SUFFIXES[day].lower() for day in bank.days or ()),
            )
            for bank in profile.banks.values()
        )
        return self._i18n.msg(
            "watcher.profile_summary",
            name=profile.name,
            banks=bank_lines,
            blackouts=blackout_lines,
        )

    async def _handle_profile_command(self, ctx: DataMessageContext, payload: str) -> None:
        payload = payload.strip()
        if not payload:
            await ctx.send(SendMessage(text=self._i18n.msg("watcher.profile_usage")))
            return

        parts = payload.split()
        subcommand = parts[0].lower()

        if subcommand in self._commands["admin_profile_define"]:
            await self._do_profile_define(ctx, payload)
            return

        handlers: dict[str, Callable[[DataMessageContext, list[str]], Awaitable[None]]] = {
            "admin_profile_list": self._do_profile_list,
            "admin_profile_show": self._do_profile_show,
            "admin_profile_use": self._do_profile_use,
        }

        for key, handler in handlers.items():
            if subcommand in self._commands[key]:
                await handler(ctx, parts)
                return

        await ctx.send(SendMessage(text=self._i18n.msg("watcher.profile_usage")))

    async def _do_profile_list(self, ctx: DataMessageContext, parts: list[str]) -> None:
        ordered = sorted(self._profiles_by_name.values(), key=lambda p: p.name.lower())
        lines = [
            f"* {profile.name}"
            for profile in ordered
        ]
        await ctx.send(SendMessage(text=self._i18n.msg("watcher.profile_list_header") + "\n" + "\n".join(lines)))

    async def _do_profile_show(self, ctx: DataMessageContext, parts: list[str]) -> None:
        if len(parts) != 2:
            await ctx.send(SendMessage(text=self._i18n.msg("watcher.profile_usage")))
            return
        profile = self._profiles_by_name.get(parts[1].lower())
        if profile is None:
            await ctx.send(SendMessage(text=self._i18n.msg("watcher.profile_unknown", profile_name=parts[1])))
            return
        await ctx.send(SendMessage(text=self._profile_summary(profile)))

    async def _do_profile_use(self, ctx: DataMessageContext, parts: list[str]) -> None:
        if len(parts) != 3:
            await ctx.send(SendMessage(text=self._i18n.msg("watcher.profile_usage")))
            return
        profile = self._profiles_by_name.get(parts[1].lower())
        if profile is None:
            await ctx.send(SendMessage(text=self._i18n.msg("watcher.profile_unknown", profile_name=parts[1])))
            return
        if parts[2] == "*":
            for phone, child in self._settings.children.items():
                self._active_profile_name_by_child[phone] = profile.name
                self._store.set_active_rule_profile_name_for_child(phone, profile.name)
                await ctx.send(SendMessage(text=self._i18n.msg("watcher.profile_switched", child_name=child.name, profile_name=profile.name)))
        else:
            resolved = await self._resolve_child(ctx, parts[2])
            if not resolved:
                return
            child, _ = resolved
            self._active_profile_name_by_child[child.phone_number] = profile.name
            self._store.set_active_rule_profile_name_for_child(child.phone_number, profile.name)
            await ctx.send(SendMessage(text=self._i18n.msg("watcher.profile_switched", child_name=child.name, profile_name=profile.name)))

    async def _do_profile_define(self, ctx: DataMessageContext, payload: str) -> None:
        parts = payload.split(maxsplit=2)
        if len(parts) != 3:
            await ctx.send(SendMessage(text=self._i18n.msg("watcher.profile_usage")))
            return
        try:
            definition = json.loads(parts[2])
            created = parse_rule_profile_definition(parts[1], definition)
        except (json.JSONDecodeError, ValueError) as exc:
            await ctx.send(
                SendMessage(text=self._i18n.msg("watcher.profile_invalid", reason=str(exc)))
            )
            return
        try:
            self._store.upsert_rule_profile(created)
        except sqlite3.Error:
            logger.exception("Failed to save rule profile %s", created.name)
            await ctx.send(SendMessage(text=self._i18n.msg("watcher.profile_save_failed", profile_name=created.name)))
            return
        self._profiles_by_name[created.name.lower()] = created
        await ctx.send(
            SendMessage(
                text=self._i18n.msg("watcher.profile_defined", profile_name=created.name)
                + "\n"
                + self._profile_summary(created)
            )
        )

    async def _run_for_resolved_children(
        self,
        ctx: DataMessageContext,
        payload: str,
        action: Callable[[Child, PlaytimeRules], Awaitable[None]],
    ) -> bool:
        resolved = await self._parse_children_payload(ctx, payload)
        if not resolved:
            return True
        for child, rules in resolved:
            await action(child, rules)
        return True

    async def _run_for_resolved_children_and_minutes(
        self,
        ctx: DataMessageContext,
        payload: str,
        parse_minutes: Callable[[str], int | None],
        action: Callable[[Child, PlaytimeRules, int, bool], Awaitable[None]],
    ) -> bool:
        resolved = await self._parse_children_and_minutes(ctx, payload, parse_minutes)
        if not resolved:
            return True
        children, minutes, is_relative = resolved
        for child, rules in children:
            await action(child, rules, minutes, is_relative)
        return True

    async def _handle_admin_command(self, ctx: DataMessageContext, message_text: str) -> bool:
        parts = message_text.split(maxsplit=1)
        if not parts:
            return False
        command = parts[0].lower()
        payload = parts[1] if len(parts) == 2 else ""

        handler = self._admin_handlers.get(command)
        if handler:
            return await handler(ctx, payload)

        return await self._handle_admin_bank_command(ctx, message_text)

    async def _send_admin_result(self, ctx: DataMessageContext, child: Child, message: str) -> None:
        await ctx.send(SendMessage(text=self._i18n.msg("watcher.admin_prefix", child_name=child.name, message=message)))

    def _format_child_status_section(self, child_id: str, child: Child) -> str:
        lines = [f"[{child.name}]"]
        if self._store.is_child_block_mode_enabled(child_id):
            lines.append(self._i18n.msg("watcher.status_child_block_mode_enabled"))
        if self._has_profile(child_id):
            lines.append(self._rules_by_child[child_id].get_status())
        else:
            lines.append(self._i18n.msg("watcher.profile_not_assigned"))
        if claims := self._store.get_pending_claims(child_id):
            lines.append(self._format_claims_response(child, claims))
        return "\n".join(lines)

    def _should_send_implicit_help(self, ctx: DataMessageContext, message_text: str) -> bool:
        """Return true only for short, likely-command messages.

        Explicit help commands always work; this only gates the noisy fallback
        when a known sender writes something the bot did not understand.
        """
        if len(message_text) > IMPLICIT_HELP_MAX_CHARS:
            return False
        if HAS_DIGIT_RE.search(message_text) is None:
            return False

        message = ctx.message
        if getattr(message, "quote", None) is not None:
            return False
        return not (getattr(message, "mentions", None) or TEXT_MENTION_RE.search(message_text))

    async def _handle_admin_block_command(self, ctx: DataMessageContext, payload: str) -> bool:
        return await self._run_for_resolved_children(ctx, payload, lambda c, r: self.admin_block_child_command(ctx, c, r))

    async def _handle_admin_unblock_command(self, ctx: DataMessageContext, payload: str) -> bool:
        return await self._run_for_resolved_children(ctx, payload, lambda c, r: self._unblock_action(ctx, c))

    async def _unblock_action(self, ctx: DataMessageContext, child: Child) -> None:
        self._store.set_child_block_mode(child.phone_number, False)
        await ctx.send(SendMessage(text=self._i18n.msg("watcher.admin_child_unblocked", child_name=child.name)))

    async def _handle_admin_test_command(self, ctx: DataMessageContext, payload: str) -> bool:
        return await self._run_for_resolved_children_and_minutes(
            ctx, payload, parse_duration_minutes, lambda c, r, m, _: self.request_command(ctx, c, r, m, c.name + " (TEST)")
        )

    async def _handle_admin_end_command(self, ctx: DataMessageContext, payload: str) -> bool:
        return await self._run_for_resolved_children(ctx, payload, lambda c, r: self.admin_end_session_command(ctx, c, r))

    async def _handle_admin_rollover_command(self, ctx: DataMessageContext, payload: str) -> bool:
        async def action(child: Child, rules: PlaytimeRules) -> None:
            if not self._has_profile(child.phone_number):
                await self._send_admin_result(ctx, child, self._i18n.msg("watcher.profile_not_assigned"))
                return
            await self._send_admin_result(ctx, child, rules.rollover_week())
        return await self._run_for_resolved_children(ctx, payload, action)

    async def _handle_admin_profile_command(self, ctx: DataMessageContext, payload: str) -> bool:
        await self._handle_profile_command(ctx, payload)
        return True

    async def _handle_admin_claims_command(self, ctx: DataMessageContext, payload: str) -> bool:
        async def action(child: Child, _rules: PlaytimeRules) -> None:
            claims = self._store.get_pending_claims(child.phone_number)
            if not claims:
                await ctx.send(SendMessage(text=self._i18n.msg("watcher.no_pending_claims", child_name=child.name)))
                return
            await ctx.send(SendMessage(text=self._format_claims_response(child, claims)))
        return await self._run_for_resolved_children(ctx, payload, action)

    async def _handle_admin_ack_command(self, ctx: DataMessageContext, payload: str) -> bool:
        async def action(child: Child, rules: PlaytimeRules) -> None:
            if not self._has_profile(child.phone_number):
                await self._send_admin_result(ctx, child, self._i18n.msg("watcher.profile_not_assigned"))
                return
            claims = self._store.get_pending_claims(child.phone_number)
            if not claims:
                await ctx.send(SendMessage(text=self._i18n.msg("watcher.no_pending_claims", child_name=child.name)))
                return
            total_minutes = sum(c.claimed_minutes for c in claims)
            self._store.mark_all_claims_handled(child.phone_number, total_minutes)
            ack_header = self._i18n.msg(
                "watcher.ack_header",
                count=len(claims),
                total=format_duration(total_minutes),
            )
            full_message = ack_header + "\n" + self._format_claims_block(claims) + "\n" + rules.modify_bank(total_minutes)
            await self._send_admin_result(ctx, child, full_message)
        return await self._run_for_resolved_children(ctx, payload, action)

    async def _handle_admin_bank_command(self, ctx: DataMessageContext, message_text: str) -> bool:
        parsed = self._parse_admin_bank_payload(message_text)
        if parsed is None:
            return False
        children, bank_name, minutes, is_relative = parsed
        for child, rules in children:
            if not self._has_profile(child.phone_number):
                await self._send_admin_result(ctx, child, self._i18n.msg("watcher.profile_not_assigned"))
                continue
            if bank_name == "*":
                pending_note = self._auto_handle_pending_claims(child)
                profile = self._profile_for_child(child.phone_number)
                results = [
                    rules.modify_bank(minutes, bkey) if is_relative else rules.set_bank(minutes, bkey)
                    for bkey in profile.banks
                ]
                await self._send_admin_result(ctx, child, "\n".join(results) + pending_note)
            else:
                pending_note = self._auto_handle_pending_claims(child) if bank_name is None else ""
                result = (
                    rules.modify_bank(minutes, bank_name)
                    if is_relative
                    else rules.set_bank(minutes, bank_name)
                )
                await self._send_admin_result(ctx, child, result + pending_note)
        return True

    def _parse_admin_bank_payload(
        self,
        payload: str,
    ) -> tuple[list[tuple[Child, PlaytimeRules]], str | None, int, bool] | None:
        parts = payload.strip().split()
        if not parts:
            return None

        def parsed_duration(text: str) -> tuple[int, bool] | None:
            minutes = parse_signed_duration_minutes(text)
            if minutes is not None:
                return minutes, not text.lstrip().startswith("=")
            if match := DAY_BANK_AMOUNT_RE.fullmatch(text.strip()):
                sign, raw_amount = match.groups()
                amount = int(raw_amount) * (-1 if sign == "-" else 1)
                return amount, sign != "="
            return None

        first_is_all_children = parts[0] == "*"
        child_rules_entry = None if first_is_all_children else self._children_by_name.get(parts[0].lower())
        first_is_child = first_is_all_children or (child_rules_entry is not None)

        if first_is_child:
            children: list[tuple[Child, PlaytimeRules]] = (
                self._all_children() if first_is_all_children else [(child_rules_entry[0], child_rules_entry[1])]  # type: ignore[index]
            )
            if len(parts) >= 3 and (parsed := parsed_duration(" ".join(parts[2:]))):
                bank_name: str | None = "*" if parts[1] == "*" else parts[1]
                return (children, bank_name, parsed[0], parsed[1])
            if len(parts) >= 2 and (parsed := parsed_duration(" ".join(parts[1:]))):
                return (children, None, parsed[0], parsed[1])
            return None

        if len(parts) >= 2 and (parsed := parsed_duration(" ".join(parts[1:]))):
            return (self._all_children(), parts[0], parsed[0], parsed[1])
        if parsed := parsed_duration(payload):
            return (self._all_children(), None, parsed[0], parsed[1])
        return None

    def setup(self) -> None:
        """Set up scheduled tasks."""
        if self.bot is None:
            raise RuntimeError("PlaytimeManager must be attached to a SignalBot before setup.")
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

        self.bot.scheduler.add_job(  # type: ignore[union-attr]
            self._check_automatic_updates,
            trigger="interval",
            minutes=1,
            timezone=self._settings.timezone,
            max_instances=1,
        )
        logger.info("Scheduled session and recovery-bank checks every minute")

    async def handle_data_message(self, context: DataMessageContext) -> None:
        """Handle incoming messages from the group."""
        raw_text = context.message.text
        if raw_text is None:
            return
        message_text = raw_text.strip()
        if not message_text:
            return
        message_lower = message_text.lower()
        sender = context.message.source_number or context.message.source
        if sender is None:
            logger.info("Ignoring Signal message without a sender")
            return
        
        is_admin = sender in self._settings.signal_admins
        child = self._settings.children.get(sender)
        child_context = (child, self._rules_by_child[sender]) if child is not None else None
        
        if not is_admin and child_context is None:
            logger.info("Ignoring message from unknown sender in group: %s", sender)
            return
        
        sender_name = child.name if child is not None else "Admin"
        
        if message_lower in self._commands["help"]:
            await context.send(
                SendMessage(
                    text=self._i18n.msg("watcher.help_admin" if is_admin else "watcher.help_child")
                )
            )
            return

        if message_lower in self._commands["status"]:
            if is_admin:
                await self._check_ms_authentication(context)
            for phone, child_obj in self._settings.children.items():
                await context.send(SendMessage(text=self._format_child_status_section(phone, child_obj)))
            return

        parsed_minutes = parse_duration_minutes(message_text)
        if parsed_minutes is not None and child_context is not None:
            child, rules = child_context
            await self.request_command(context, child, rules, parsed_minutes, sender_name)
            return

        if child_context is not None:
            child, rules = child_context
            claim_minutes = parse_activity_claim(message_text)
            if claim_minutes is not None:
                await self.claim_command(context, child, claim_minutes, message_text, sender_name)
                return

        if child_context is not None and NUMERIC_ONLY_RE.match(message_text):
            await context.send(
                SendMessage(
                    text=self._i18n.msg("watcher.numeric_unit_required", sender_name=sender_name)
                )
            )
            return

        if message_lower in self._commands["end"] and child_context is not None:
            child, rules = child_context
            await self.end_session_command(context, child, rules, sender_name)
            return

        if is_admin and await self._handle_admin_command(context, message_text):
            return
        
        if self._should_send_implicit_help(context, message_text):
            await context.send(
                SendMessage(
                    text=self._i18n.msg("watcher.help_admin" if is_admin else "watcher.help_child")
                )
            )

    async def _call_ms_api_with_retry(
        self,
        action_name: str,
        action: Callable[[MicrosoftFamilyApi], Awaitable[bool]],
        mfa_challenge_handler: Callable[[str, int], Awaitable[None]] | None = None,
    ) -> bool:
        async with self._ms_api_lock:
            if self._ms_api is None:
                self._ms_api = MicrosoftFamilyApi(
                    self._settings.ms_family_email,
                    session_path=Path(self._settings.data_dir) / "ms-family-session.json",
                )
            api = self._ms_api
            await api.ensure_authenticated(mfa_challenge_handler=mfa_challenge_handler)
            if await action(api):
                return True
            if api.last_status_code != 401:
                return False
            logger.info(
                "Microsoft %s API returned 401; retrying once with forced re-authentication.",
                action_name,
            )
            await api.ensure_authenticated(force=True, mfa_challenge_handler=mfa_challenge_handler)
            return await action(api)

    async def _check_ms_authentication(self, ctx: DataMessageContext) -> None:
        async def announce_mfa(display_id: str, timeout_seconds: int) -> None:
            await ctx.send(SendMessage(text=self._i18n.msg(
                "watcher.ms_authenticator_challenge",
                action=self._i18n.msg("watcher.ms_authenticator_action_auth"),
                display_id=display_id,
                timeout_seconds=timeout_seconds,
            )))

        async def authentication_only(_api: MicrosoftFamilyApi) -> bool:
            return True

        try:
            await self._call_ms_api_with_retry(
                "authentication check",
                authentication_only,
                announce_mfa,
            )
        except (ValueError, RuntimeError, httpx.HTTPError) as exc:
            logger.error("Microsoft authentication check failed: %s", exc)
            await ctx.send(SendMessage(text=self._i18n.msg("watcher.auth_failed")))
            return

        await ctx.send(SendMessage(text=self._i18n.msg("watcher.auth_succeeded")))

    async def _grant_via_ms_api(
        self,
        child_id: str,
        minutes: int,
        mfa_challenge_handler: Callable[[str, int], Awaitable[None]] | None = None,
    ) -> bool:
        return await self._call_ms_api_with_retry(
            "grant",
            lambda api: api.grant_screen_time(child_id, minutes),
            mfa_challenge_handler,
        )

    async def _block_via_ms_api(
        self,
        child_id: str,
        mfa_challenge_handler: Callable[[str, int], Awaitable[None]] | None = None,
    ) -> bool:
        return await self._call_ms_api_with_retry(
            "block",
            lambda api: api.block_screen_time(child_id),
            mfa_challenge_handler,
        )

    async def _complete_session_after_block(
        self,
        ctx: DataMessageContext,
        child: Child,
        rules: PlaytimeRules,
        error_message: str,
        rejected_message: str,
        prefix: str = "",
        complete_if_inactive: bool = True,
    ) -> str | None:
        """Block an active session before completing it; return None if block failed."""
        if not rules.has_active_session():
            return rules.complete_session() if complete_if_inactive else ""
        async def announce_mfa(display_id: str, timeout_seconds: int) -> None:
            await ctx.send(SendMessage(text=self._i18n.msg(
                "watcher.ms_authenticator_challenge",
                action=self._i18n.msg("watcher.ms_authenticator_action_block", child_name=child.name),
                display_id=display_id,
                timeout_seconds=timeout_seconds,
            )))
        try:
            block_success = await self._block_via_ms_api(child.ms_account_id, announce_mfa)
        except (ValueError, RuntimeError, httpx.HTTPError) as exc:
            logger.error("Failed to block time via Microsoft API: %s", exc)
            await ctx.send(SendMessage(text=prefix + error_message))
            return None
        if not block_success:
            await ctx.send(SendMessage(text=prefix + rejected_message))
            return None
        return rules.complete_session()

    async def request_command(
        self,
        ctx: DataMessageContext,
        child: Child,
        rules: PlaytimeRules,
        minutes: int,
        sender_name: str,
    ) -> None:
        """Handle a playtime request from child."""
        if minutes <= 0:
            await self._send_with_pending_claims(ctx, child, self._i18n.msg("watcher.positive_duration_required", sender_name=sender_name))
            return

        if not self._has_profile(child.phone_number):
            await self._send_with_pending_claims(
                ctx, child, self._i18n.msg(
                    "watcher.request_denied",
                    sender_name=sender_name,
                    reason=self._i18n.msg("watcher.profile_not_assigned"),
                )
            )
            return

        if self._store.is_child_block_mode_enabled(child.phone_number):
            await self._send_with_pending_claims(
                ctx, child, self._i18n.msg(
                    "watcher.request_denied",
                    sender_name=sender_name,
                    reason=self._i18n.msg("watcher.request_blocked_by_admin"),
                )
            )
            return

        decision = rules.evaluate_request(minutes)
        if not decision.allowed:
            await self._send_with_pending_claims(ctx, child, self._i18n.msg("watcher.request_denied", sender_name=sender_name, reason=decision.reason))
            return

        async def announce_mfa(display_id: str, timeout_seconds: int) -> None:
            await ctx.send(SendMessage(text=self._i18n.msg(
                "watcher.ms_authenticator_challenge",
                action=self._i18n.msg(
                    "watcher.ms_authenticator_action_grant",
                    child_name=child.name,
                    minutes=format_duration(decision.minutes_granted),
                ),
                display_id=display_id,
                timeout_seconds=timeout_seconds,
            )))
        try:
            grant_success = await self._grant_via_ms_api(
                child.ms_account_id,
                decision.minutes_granted,
                announce_mfa,
            )
        except (ValueError, RuntimeError, httpx.HTTPError) as exc:
            logger.error("Failed to grant time via Microsoft API: %s", exc)
            await self._send_with_pending_claims(ctx, child, self._i18n.msg("watcher.request_grant_error", sender_name=sender_name))
            return
        if not grant_success:
            await self._send_with_pending_claims(ctx, child, self._i18n.msg("watcher.request_grant_failed", sender_name=sender_name))
            return

        _, message = rules.grant_playtime(decision.minutes_granted)
        response = f"[{sender_name}]"
        if decision.reason:
            response += f"\n{decision.reason}"
        response += f"\n{message}"
        await self._send_with_pending_claims(ctx, child, response)

    async def admin_block_child_command(self, ctx: DataMessageContext, child: Child, rules: PlaytimeRules) -> None:
        """Enable grant block mode for a child and end any active session."""
        self._store.set_child_block_mode(child.phone_number, True)
        block_msg = self._i18n.msg("watcher.admin_child_blocked", child_name=child.name)
        if not self._has_profile(child.phone_number):
            await ctx.send(SendMessage(text=block_msg))
            return
        result = await self._complete_session_after_block(
            ctx,
            child,
            rules,
            self._i18n.msg("watcher.admin_end_block_error", child_name=child.name),
            self._i18n.msg("watcher.admin_end_block_rejected", child_name=child.name),
            block_msg + "\n",
            complete_if_inactive=False,
        )
        if result is None:
            return
        if not result:
            await ctx.send(SendMessage(text=block_msg))
            return
        await ctx.send(
            SendMessage(
                text=block_msg + "\n"
                + self._i18n.msg("watcher.admin_end_session", child_name=child.name, result=result)
            )
        )

    async def admin_end_session_command(self, ctx: DataMessageContext, child: Child, rules: PlaytimeRules) -> None:
        """Admin end active session."""
        if not self._has_profile(child.phone_number):
            await self._send_admin_result(ctx, child, self._i18n.msg("watcher.profile_not_assigned"))
            return
        result = await self._complete_session_after_block(
            ctx,
            child,
            rules,
            self._i18n.msg("watcher.admin_end_block_error", child_name=child.name),
            self._i18n.msg("watcher.admin_end_block_rejected", child_name=child.name),
        )
        if result is None:
            return
        await ctx.send(SendMessage(text=self._i18n.msg("watcher.admin_end_session", child_name=child.name, result=result)))

    async def end_session_command(
        self,
        ctx: DataMessageContext,
        child: Child,
        rules: PlaytimeRules,
        sender_name: str,
    ) -> None:
        if not self._has_profile(child.phone_number):
            await ctx.send(SendMessage(text=f"[{sender_name}]\n" + self._i18n.msg("watcher.profile_not_assigned")))
            return
        result = await self._complete_session_after_block(
            ctx,
            child,
            rules,
            self._i18n.msg("watcher.end_block_error", sender_name=sender_name),
            self._i18n.msg("watcher.end_block_failed", sender_name=sender_name),
        )
        if result is None:
            return
        await ctx.send(SendMessage(text=f"[{sender_name}]\n{result}"))

    async def _check_weekly_rollover(self) -> None:
        """Scheduled task to handle weekly rollover for all children."""
        results = [
            f"{child.name}: {self._rules_by_child[phone].rollover_week()}"
            for phone, child in self._settings.children.items()
            if self._has_profile(phone)
        ]
        for result in results:
            logger.info("Weekly rollover for %s", result)
        
        # Send to group
        try:
            combined_message = self._i18n.msg("watcher.weekly_rollover_header") + "\n\n" + "\n\n".join(results)
            await self._send_group(combined_message)
        except Exception:
            logger.exception("Failed to send rollover notification to group")

    async def _check_automatic_updates(self) -> None:
        """Notify when sessions finish naturally or recovery banks refill."""
        results = []
        for phone, child in self._settings.children.items():
            rules = self._rules_by_child[phone]
            if not self._has_profile(phone):
                continue
            child_msgs = rules.check_automatic_updates()
            if child_msgs:
                results.append(f"[{child.name}]\n" + "\n".join(child_msgs))

        if not results:
            return

        try:
            await self._send_group("\n\n".join(results))
        except Exception:
            logger.exception("Failed to send scheduled notification to group")

    async def _send_group(self, text: str) -> None:
        if self.bot is None:
            raise RuntimeError("PlaytimeManager is not attached to a SignalBot.")
        await self.bot.messages.send(SendMessage(text=text), self._settings.signal_group_id)

    async def claim_command(
        self,
        ctx: DataMessageContext,
        child: Child,
        minutes: int,
        description: str,
        sender_name: str,
    ) -> None:
        """Record an activity claim from a child."""
        self._store.add_activity_claim(
            child.phone_number, minutes, description, datetime.now(timezone.utc)
        )
        await self._send_with_pending_claims(
            ctx, child, self._i18n.msg(
                "watcher.claim_received",
                sender_name=sender_name,
                minutes=format_duration(minutes),
                description=description,
            )
        )

    async def _send_with_pending_claims(self, ctx: DataMessageContext, child: Child, message: str) -> None:
        if claims := self._store.get_pending_claims(child.phone_number):
            message += "\n" + self._format_claims_response(child, claims)
        await ctx.send(SendMessage(text=message))

    def _format_claims_block(self, claims: list[ActivityClaim]) -> str:
        """Format claim entries + total as a multi-line string (no header)."""
        total = sum(c.claimed_minutes for c in claims)
        return "\n".join(
            [
                *(
                    self._i18n.msg(
                        "watcher.claims_entry",
                        description=c.description,
                        submitted_at=c.submitted_at.astimezone(self._tz).strftime("%Y-%m-%d %H:%M"),
                    )
                    for c in claims
                ),
                self._i18n.msg("watcher.claims_total", total=format_duration(total)),
            ]
        )

    def _format_claims_response(self, child: Child, claims: list[ActivityClaim]) -> str:
        """Format pending claims exactly as the claims command displays them."""
        header = self._i18n.msg("watcher.claims_header", child_name=child.name, count=len(claims))
        return header + "\n" + self._format_claims_block(claims)

    def _auto_handle_pending_claims(self, child: Child) -> str:
        """Mark pending claims as handled; return a formatted note (or empty string)."""
        claims = self._store.get_pending_claims(child.phone_number)
        if not claims:
            return ""
        self._store.mark_all_claims_handled(child.phone_number)
        header = self._i18n.msg("watcher.claims_auto_handled_header", count=len(claims))
        return "\n" + header + "\n" + self._format_claims_block(claims)
