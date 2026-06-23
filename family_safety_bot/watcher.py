from __future__ import annotations

import asyncio
import logging
import re
import shlex
from datetime import datetime, timezone, timedelta
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
HAS_DIGIT_RE = re.compile(r"\d")
TEXT_MENTION_RE = re.compile(r"(^|\s)@\S+")
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
_PROFILE_REQUIRED_ENV_KEYS = (
    "WEEKLY_ADDITION_TIME",
    "MAX_BANK_TIME",
    "ACCRUED_PLAYTIME_MAX_TIME",
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
        self._profiles_by_name = {profile.name.lower(): profile for profile in existing}
        default_key = self._default_profile.name.lower()
        if default_key not in self._profiles_by_name:
            self._profiles_by_name[default_key] = self._default_profile
            self._store.upsert_rule_profile(self._default_profile)

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
        if not (stripped := payload.strip()):
            return None

        parts = stripped.split(maxsplit=1)
        if len(parts) == 2:
            child_name, duration_text = parts
            if (minutes := parse_minutes(duration_text)) is not None:
                if resolved := await self._resolve_child(ctx, child_name):
                    return ([(resolved[0], resolved[1])], minutes, not duration_text.lstrip().startswith("="))

        if (minutes := parse_minutes(stripped)) is None:
            return None

        return (self._all_children(), minutes, not stripped.lstrip().startswith("="))

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
        return self._i18n.msg(
            "watcher.profile_summary",
            name=profile.name,
            weekly_addition=format_duration(profile.weekly_addition_minutes),
            max_bank=format_duration(profile.max_bank_minutes),
            accrued_max=format_duration(profile.accrued_playtime_max_minutes),
            break_recovery_rate=f"{profile.break_recovery_rate:.2f}",
            blackouts=blackout_lines,
        )

    @staticmethod
    def _parse_env_assignments(payload: str) -> dict[str, str] | None:
        try:
            tokens = shlex.split(payload)
        except ValueError:
            return None
        assignments = {}
        for token in tokens:
            if "=" not in token:
                return None
            key, value = token.split("=", 1)
            if not (key := key.strip()):
                return None
            assignments[key] = value.strip()
        return assignments

    @staticmethod
    def _positive_duration_assignment(assignments: dict[str, str], key: str) -> int:
        if minutes := parse_duration_minutes(assignments[key]):
            if minutes > 0:
                return minutes
        raise ValueError

    def _build_profile_from_env_assignments(self, profile_name: str, assignments: dict[str, str]) -> RuleProfile:
        if missing := set(_PROFILE_REQUIRED_ENV_KEYS) - set(assignments):
            raise ValueError(f"Missing required keys: {missing}")

        def get_duration(key: str) -> int:
            if (minutes := parse_duration_minutes(assignments[key])) and minutes > 0:
                return minutes
            raise ValueError

        blackout_periods = [
            period
            for suffix in WEEKDAY_SUFFIXES
            if (day_value := assignments.get(f"BLACKOUT_PERIOD_{suffix}", ""))
            for period in parse_day_blackout_periods(
                day_value,
                WEEKDAY_NAME_TO_INDEX[suffix.lower()],
                f"profile env BLACKOUT_PERIOD_{suffix}",
            )
        ]

        try:
            break_recovery_rate = float(assignments["BREAK_RECOVERY_RATE"])
            if break_recovery_rate <= 0:
                raise ValueError
        except ValueError:
            raise ValueError("Invalid BREAK_RECOVERY_RATE")

        return RuleProfile(
            name=profile_name,
            weekly_addition_minutes=get_duration("WEEKLY_ADDITION_TIME"),
            max_bank_minutes=get_duration("MAX_BANK_TIME"),
            accrued_playtime_max_minutes=get_duration("ACCRUED_PLAYTIME_MAX_TIME"),
            break_recovery_rate=break_recovery_rate,
            blackout_periods=blackout_periods,
        )

    async def _handle_profile_command(self, ctx: Context, payload: str) -> None:
        payload = payload.strip()
        if not payload:
            await ctx.send(self._i18n.msg("watcher.profile_usage"))
            return

        parts = payload.split()
        subcommand = parts[0].lower()

        if subcommand in self._commands["admin_profile_define"]:
            await self._do_profile_define(ctx, payload)
            return

        handlers: dict[str, Callable[[Context, list[str]], Awaitable[None]]] = {
            "admin_profile_list": self._do_profile_list,
            "admin_profile_show": self._do_profile_show,
            "admin_profile_use": self._do_profile_use,
        }

        for key, handler in handlers.items():
            if subcommand in self._commands[key]:
                await handler(ctx, parts)
                return

        await ctx.send(self._i18n.msg("watcher.profile_usage"))

    async def _do_profile_list(self, ctx: Context, parts: list[str]) -> None:
        ordered = sorted(self._profiles_by_name.values(), key=lambda p: p.name.lower())
        lines = [
            f"* {profile.name}"
            + (self._i18n.msg("watcher.profile_default_marker") if profile.name.lower() == self._default_profile.name.lower() else "")
            for profile in ordered
        ]
        await ctx.send(self._i18n.msg("watcher.profile_list_header") + "\n" + "\n".join(lines))

    async def _do_profile_show(self, ctx: Context, parts: list[str]) -> None:
        if len(parts) != 2:
            await ctx.send(self._i18n.msg("watcher.profile_usage"))
            return
        profile = self._profiles_by_name.get(parts[1].lower())
        if profile is None:
            await ctx.send(self._i18n.msg("watcher.profile_unknown", profile_name=parts[1]))
            return
        await ctx.send(self._profile_summary(profile))

    async def _do_profile_use(self, ctx: Context, parts: list[str]) -> None:
        if len(parts) != 3:
            await ctx.send(self._i18n.msg("watcher.profile_usage"))
            return
        profile = self._profiles_by_name.get(parts[1].lower())
        if profile is None:
            await ctx.send(self._i18n.msg("watcher.profile_unknown", profile_name=parts[1]))
            return
        resolved = await self._resolve_child(ctx, parts[2])
        if not resolved:
            return
        child, _ = resolved
        self._active_profile_name_by_child[child.phone_number] = profile.name
        self._store.set_active_rule_profile_name_for_child(child.phone_number, profile.name)
        await ctx.send(self._i18n.msg("watcher.profile_switched", child_name=child.name, profile_name=profile.name))

    async def _do_profile_define(self, ctx: Context, payload: str) -> None:
        parts = payload.split(maxsplit=2)
        if len(parts) != 3:
            await ctx.send(self._i18n.msg("watcher.profile_usage"))
            return
        try:
            name = normalize_profile_name(parts[1])
        except ValueError:
            await ctx.send(self._i18n.msg("watcher.profile_invalid"))
            return
        assignments = self._parse_env_assignments(parts[2])
        if assignments is None:
            await ctx.send(self._i18n.msg("watcher.profile_invalid"))
            return
        try:
            created = self._build_profile_from_env_assignments(name, assignments)
        except ValueError:
            await ctx.send(self._i18n.msg("watcher.profile_invalid"))
            return
        self._store.upsert_rule_profile(created)
        self._profiles_by_name[name.lower()] = created
        await ctx.send(
            self._i18n.msg("watcher.profile_defined", profile_name=name)
            + "\n"
            + self._profile_summary(created)
        )

    async def _run_for_resolved_children(
        self,
        ctx: Context,
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
        ctx: Context,
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

    async def _handle_admin_command(self, ctx: Context, message_text: str) -> bool:
        parts = message_text.split(maxsplit=1)
        if not parts:
            return False
        command = parts[0].lower()
        payload = parts[1] if len(parts) == 2 else ""

        handler = self._admin_handlers.get(command)
        if handler:
            return await handler(ctx, payload)

        return await self._handle_admin_bank_command(ctx, message_text)

    async def _send_admin_result(self, ctx: Context, child: Child, message: str) -> None:
        await ctx.send(self._i18n.msg("watcher.admin_prefix", child_name=child.name, message=message))

    def _format_child_status_section(self, child_id: str, child: Child) -> str:
        lines = [f"[{child.name}]"]
        if self._store.is_child_block_mode_enabled(child_id):
            lines.append(self._i18n.msg("watcher.status_child_block_mode_enabled"))
        lines.append(self._rules_by_child[child_id].get_status())
        if claims := self._store.get_pending_claims(child_id):
            lines.append(self._format_claims_response(child, claims))
        return "\n".join(lines)

    def _should_send_implicit_help(self, ctx: Context, message_text: str) -> bool:
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

    async def _handle_admin_block_command(self, ctx: Context, payload: str) -> bool:
        return await self._run_for_resolved_children(ctx, payload, lambda c, r: self.admin_block_child_command(ctx, c, r))

    async def _handle_admin_unblock_command(self, ctx: Context, payload: str) -> bool:
        return await self._run_for_resolved_children(ctx, payload, lambda c, r: self._unblock_action(ctx, c))

    async def _unblock_action(self, ctx: Context, child: Child) -> None:
        self._store.set_child_block_mode(child.phone_number, False)
        await ctx.send(self._i18n.msg("watcher.admin_child_unblocked", child_name=child.name))

    async def _handle_admin_test_command(self, ctx: Context, payload: str) -> bool:
        return await self._run_for_resolved_children_and_minutes(
            ctx, payload, parse_duration_minutes, lambda c, r, m, _: self.request_command(ctx, c, r, m, c.name + " (TEST)")
        )

    async def _handle_admin_end_command(self, ctx: Context, payload: str) -> bool:
        return await self._run_for_resolved_children(ctx, payload, lambda c, r: self.admin_end_session_command(ctx, c, r))

    async def _handle_admin_rollover_command(self, ctx: Context, payload: str) -> bool:
        async def action(child: Child, rules: PlaytimeRules) -> None:
            await self._send_admin_result(ctx, child, rules.rollover_week())
        return await self._run_for_resolved_children(ctx, payload, action)

    async def _handle_admin_profile_command(self, ctx: Context, payload: str) -> bool:
        await self._handle_profile_command(ctx, payload)
        return True

    async def _handle_admin_claims_command(self, ctx: Context, payload: str) -> bool:
        async def action(child: Child, _rules: PlaytimeRules) -> None:
            claims = self._store.get_pending_claims(child.phone_number)
            if not claims:
                await ctx.send(self._i18n.msg("watcher.no_pending_claims", child_name=child.name))
                return
            await ctx.send(self._format_claims_response(child, claims))
        return await self._run_for_resolved_children(ctx, payload, action)

    async def _handle_admin_ack_command(self, ctx: Context, payload: str) -> bool:
        async def action(child: Child, rules: PlaytimeRules) -> None:
            claims = self._store.get_pending_claims(child.phone_number)
            if not claims:
                await ctx.send(self._i18n.msg("watcher.no_pending_claims", child_name=child.name))
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

    async def _handle_admin_bank_command(self, ctx: Context, message_text: str) -> bool:
        resolved = await self._parse_children_and_minutes(ctx, message_text, parse_signed_duration_minutes)
        if not resolved:
            return False
        children, minutes, is_relative = resolved
        for child, rules in children:
            pending_note = self._auto_handle_pending_claims(child)
            result = rules.modify_bank(minutes) if is_relative else rules.set_bank(minutes)
            await self._send_admin_result(ctx, child, result + pending_note)
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

        self.bot.scheduler.add_job(  # type: ignore[union-attr]
            self._check_recovery_completion,
            trigger="interval",
            minutes=1,
            timezone=self._settings.timezone,
            max_instances=1,
        )
        logger.info("Scheduled recovery completion check every minute")

    async def handle(self, context: Context) -> None:
        """Handle incoming messages from the group."""
        raw_text = context.message.text
        if raw_text is None:
            return
        message_text = raw_text.strip()
        if not message_text:
            return
        message_lower = message_text.lower()
        sender = context.message.source
        
        is_admin = sender in self._settings.signal_admins
        child = self._settings.children.get(sender)
        child_context = (child, self._rules_by_child[sender]) if child is not None else None
        
        if not is_admin and child_context is None:
            logger.info("Ignoring message from unknown sender in group: %s", sender)
            return
        
        sender_name = child.name if child is not None else "Admin"
        
        if message_lower in self._commands["help"]:
            await context.send(self._i18n.msg("watcher.help_admin" if is_admin else "watcher.help_child"))
            return

        if message_lower in self._commands["status"]:
            child_sections = [
                self._format_child_status_section(phone, child_obj)
                for phone, child_obj in self._settings.children.items()
            ]
            await context.send("\n\n".join(child_sections))
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
            await context.send(self._i18n.msg("watcher.numeric_unit_required", sender_name=sender_name))
            return

        if message_lower in self._commands["end"] and child_context is not None:
            child, rules = child_context
            await self.end_session_command(context, child, rules, sender_name)
            return

        if is_admin and await self._handle_admin_command(context, message_text):
            return
        
        if self._should_send_implicit_help(context, message_text):
            await context.send(self._i18n.msg("watcher.help_admin" if is_admin else "watcher.help_child"))

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
            if await action(api):
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

    async def _complete_session_after_block(
        self,
        ctx: Context,
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
        try:
            block_success = await self._block_via_ms_api(child.ms_account_id)
        except (ValueError, RuntimeError, httpx.HTTPError):
            logger.exception("Failed to block time via Microsoft API")
            await ctx.send(prefix + error_message)
            return None
        if not block_success:
            await ctx.send(prefix + rejected_message)
            return None
        return rules.complete_session()

    async def request_command(
        self,
        ctx: Context,
        child: Child,
        rules: PlaytimeRules,
        minutes: int,
        sender_name: str,
    ) -> None:
        """Handle a playtime request from child."""
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

        decision = rules.evaluate_request(minutes)
        if not decision.allowed:
            await ctx.send(self._i18n.msg("watcher.request_denied", sender_name=sender_name, reason=decision.reason))
            return

        try:
            grant_success = await self._grant_via_ms_api(child.ms_account_id, decision.minutes_granted)
        except (ValueError, RuntimeError, httpx.HTTPError):
            logger.exception("Failed to grant time via Microsoft API")
            await ctx.send(self._i18n.msg("watcher.request_grant_error", sender_name=sender_name))
            return
        if not grant_success:
            await ctx.send(self._i18n.msg("watcher.request_grant_failed", sender_name=sender_name))
            return

        _, message = rules.grant_playtime(decision.minutes_granted)
        response = f"[{sender_name}]"
        if decision.reason:
            response += f"\n{decision.reason}"
        response += f"\n{message}"
        await ctx.send(response)

    async def admin_block_child_command(self, ctx: Context, child: Child, rules: PlaytimeRules) -> None:
        """Enable grant block mode for a child and end any active session."""
        self._store.set_child_block_mode(child.phone_number, True)
        block_msg = self._i18n.msg("watcher.admin_child_blocked", child_name=child.name)
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
            await ctx.send(block_msg)
            return
        await ctx.send(
            block_msg + "\n"
            + self._i18n.msg("watcher.admin_end_session", child_name=child.name, result=result)
        )

    async def admin_end_session_command(self, ctx: Context, child: Child, rules: PlaytimeRules) -> None:
        """Admin end active session."""
        result = await self._complete_session_after_block(
            ctx,
            child,
            rules,
            self._i18n.msg("watcher.admin_end_block_error", child_name=child.name),
            self._i18n.msg("watcher.admin_end_block_rejected", child_name=child.name),
        )
        if result is None:
            return
        await ctx.send(self._i18n.msg("watcher.admin_end_session", child_name=child.name, result=result))

    async def end_session_command(
        self,
        ctx: Context,
        child: Child,
        rules: PlaytimeRules,
        sender_name: str,
    ) -> None:
        result = await self._complete_session_after_block(
            ctx,
            child,
            rules,
            self._i18n.msg("watcher.end_block_error", sender_name=sender_name),
            self._i18n.msg("watcher.end_block_failed", sender_name=sender_name),
        )
        if result is None:
            return
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

    async def _check_recovery_completion(self) -> None:
        """Scheduled task to notify when accrued playtime recovery finishes or playtime finishes naturally."""
        results = []
        for phone, child in self._settings.children.items():
            rules = self._rules_by_child[phone]
            local_now = rules._get_local_now()
            
            # Check if active session has expired naturally
            active = self._store.get_active_session(child.phone_number)
            session_finished_message = None
            if active:
                session_id, start_time, minutes_granted = active
                session_end = start_time + timedelta(minutes=minutes_granted)
                if local_now >= session_end:
                    session_finished_message = self._i18n.msg(
                        "rules.session_completed",
                        minutes=format_duration(minutes_granted),
                    )
            
            recovery_message = rules.check_recovery_completion()
            
            child_msgs = []
            if session_finished_message:
                child_msgs.append(session_finished_message)
            if recovery_message:
                child_msgs.append(recovery_message)
                
            if child_msgs:
                results.append(f"[{child.name}]\n" + "\n".join(child_msgs))

        if not results:
            return

        try:
            await self.bot.send(self._settings.signal_group_id, "\n\n".join(results))  # type: ignore[union-attr]
        except Exception:
            logger.exception("Failed to send scheduled notification to group")

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
        total = sum(c.claimed_minutes for c in claims)
        return "\n".join(
            [
                *(
                    self._i18n.msg(
                        "watcher.claims_entry",
                        description=c.description,
                        submitted_at=c.submitted_at.strftime("%Y-%m-%d %H:%M"),
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
