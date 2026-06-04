from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Callable

from dateutil import tz

from family_safety_bot.config import RuleProfile, Settings, parse_clock_minutes
from family_safety_bot.formatting import format_duration
from family_safety_bot.i18n import I18n
from family_safety_bot.storage import ActiveSession, PlaytimeStore


@dataclass
class PlaytimeDecision:
    """Result of evaluating a playtime request."""
    
    allowed: bool
    reason: str
    minutes_granted: int = 0


@dataclass
class RuntimeState:
    """Precomputed runtime state used by rule evaluation/grant/status paths."""

    bank_balance: int
    weekly_bank_baseline: int
    consumed_break_debt: int
    rest_accumulated: float
    active_remaining: int
    active_session: ActiveSession | None
    effective_break_load: int


class PlaytimeRules:
    """Enforce playtime rules and manage weekly allowances for a specific child."""
    
    def __init__(
        self,
        child_id: str,
        settings: Settings,
        store: PlaytimeStore,
        profile_provider: Callable[[], RuleProfile],
        i18n: I18n | None = None,
    ) -> None:
        self._child_id = child_id
        self._settings = settings
        self._store = store
        self._profile_provider = profile_provider
        self._tz = tz.gettz(settings.timezone)
        self._i18n = i18n or I18n("en")

    def _profile(self) -> RuleProfile:
        return self._profile_provider()

    @staticmethod
    @lru_cache(maxsize=None)
    def _parse_blackout_periods(
        blackout_periods: tuple[tuple[int, str, str], ...],
    ) -> tuple[tuple[int, int, int, str, str], ...]:
        parsed: list[tuple[int, int, int, str, str]] = []
        for weekday, start_time, end_time in blackout_periods:
            start_minutes = parse_clock_minutes(start_time)
            end_minutes = parse_clock_minutes(end_time)
            if start_minutes is None or end_minutes is None:
                continue
            parsed.append((weekday, start_minutes, end_minutes, start_time, end_time))
        return tuple(parsed)

    def _parsed_blackout_periods(self, profile: RuleProfile) -> tuple[tuple[int, int, int, str, str], ...]:
        return self._parse_blackout_periods(tuple(profile.blackout_periods))

    def _get_local_now(self) -> datetime:
        """Get current time in configured timezone."""
        return datetime.now(self._tz)

    @staticmethod
    def _elapsed_minutes(start_time: datetime, now: datetime) -> int:
        return max(0, int((now - start_time).total_seconds() / 60))

    def _recovery_minutes(self, break_balance_minutes: int, profile: RuleProfile) -> int:
        return int(break_balance_minutes / profile.break_recovery_rate)

    def _current_week_start(self, now: datetime) -> datetime:
        return (now - timedelta(days=now.weekday())).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

    def _current_week_start_iso(self, now: datetime) -> str:
        return self._current_week_start(now).isoformat()

    def _weekly_bank_baseline(self, now: datetime, bank_balance: int) -> int:
        return self._store.get_weekly_bank_baseline(
            self._child_id,
            self._current_week_start_iso(now),
            bank_balance,
        )

    def _bank_pace_metrics(
        self,
        minutes: int,
        weekly_baseline_minutes: int,
        now: datetime,
        profile: RuleProfile,
    ) -> tuple[int, float, float, float]:
        playable_days_remaining, non_blackout_minutes_remaining = self._remaining_playable_window(now, profile)
        if playable_days_remaining <= 0:
            avg_minutes_per_day = 0
        else:
            avg_minutes_per_day = int(round(minutes / playable_days_remaining))

        week_non_blackout_minutes = self._this_week_non_blackout_minutes(now, profile)
        weekly_playtime_minutes = weekly_baseline_minutes
        playtime_share = (minutes / weekly_playtime_minutes) if weekly_playtime_minutes > 0 else 0.0
        non_blackout_share = (
            non_blackout_minutes_remaining / week_non_blackout_minutes
            if week_non_blackout_minutes > 0
            else 0.0
        )
        pace_ratio = (playtime_share / non_blackout_share) if non_blackout_share > 0 else 0.0
        return avg_minutes_per_day, playtime_share, non_blackout_share, pace_ratio

    def _bank_pace_line(self, minutes: int, weekly_baseline_minutes: int, now: datetime, profile: RuleProfile) -> str:
        avg_minutes_per_day, playtime_share, non_blackout_share, pace_ratio = self._bank_pace_metrics(
            minutes,
            weekly_baseline_minutes,
            now,
            profile,
        )
        return self._i18n.msg(
            "rules.status_bank_avg_pace",
            avg_per_day=format_duration(avg_minutes_per_day),
            playtime_share=f"{playtime_share * 100:.1f}%",
            non_blackout_share=f"{non_blackout_share * 100:.1f}%",
            pace_ratio=f"{pace_ratio:.2f}x",
        )

    def _update_break_balance(self, now: datetime, profile: RuleProfile) -> None:
        """Update break balance based on time passed since last update.

        Break balance tracks consumed playtime debt:
        - increases during active playtime minutes
        - decreases only after a complete recovery period (rest_accumulated >= debt / break_recovery_rate)
        - any new play resets the rest accumulation counter
        """
        consumed_break_debt, last_update, rest_accumulated = self._store.get_consumed_break_debt(self._child_id)
        time_passed = (now - last_update).total_seconds() / 60  # minutes
        if time_passed <= 0:
            return  # No time has passed

        play_minutes = 0.0
        play_end = last_update
        active = self._store.get_active_session(self._child_id)
        if active:
            session_id, start_time, minutes_granted = active
            session_end = start_time + timedelta(minutes=minutes_granted)

            if now >= session_end:
                # Session expired naturally; mark it complete at actual end time.
                self._store.complete_session(session_id, session_end)

            # Count only the overlap between [last_update, now] and the session interval.
            play_start = max(last_update, start_time)
            play_end_time = min(now, session_end)
            if play_end_time > play_start:
                play_minutes = (play_end_time - play_start).total_seconds() / 60
                play_end = play_end_time

        if play_minutes > 0:
            # Play occurred: add debt and reset rest accumulation.
            # Any rest after the session ended starts a fresh accumulation period.
            new_debt = consumed_break_debt + play_minutes
            new_rest_accumulated = (now - play_end).total_seconds() / 60 if play_end < now else 0.0
        else:
            non_play_minutes = max(0.0, time_passed - play_minutes)
            new_debt = float(consumed_break_debt)
            new_rest_accumulated = rest_accumulated + non_play_minutes

        # Recovery is only granted when a full rest period is completed.
        if new_debt > 0:
            recovery_needed = new_debt / profile.break_recovery_rate
            if new_rest_accumulated >= recovery_needed:
                new_debt = 0.0
                new_rest_accumulated = 0.0

        self._store.set_consumed_break_debt(self._child_id, int(new_debt), now, new_rest_accumulated)

    def _is_in_blackout_period(self, dt: datetime, profile: RuleProfile | None = None) -> tuple[bool, str]:
        """Check if given time is in a blackout period.
        
        Returns (is_blackout, reason).
        """
        effective_profile = profile or self._profile()
        weekday = dt.weekday()  # 0=Monday, 6=Sunday

        # Check specific time periods
        current_minutes = dt.hour * 60 + dt.minute
        for period_weekday, start_minutes, end_minutes, start_time, end_time in self._parsed_blackout_periods(effective_profile):
            if weekday != period_weekday:
                continue

            if start_minutes <= current_minutes < end_minutes:
                return (
                    True,
                    self._i18n.msg(
                        "rules.blackout_denied",
                        start_time=start_time,
                        end_time=end_time,
                    ),
                )
        
        return (False, "")

    def _minutes_until_next_blackout(self, dt: datetime, profile: RuleProfile | None = None) -> int | None:
        """Return minutes until next blackout starts, or None if no blackout periods exist."""
        effective_profile = profile or self._profile()
        parsed_blackouts = self._parsed_blackout_periods(effective_profile)
        if not parsed_blackouts:
            return None

        now = dt
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        best_minutes: int | None = None

        # Check occurrences in the next 7 days (inclusive of today).
        for day_offset in range(8):
            target_day = (now.weekday() + day_offset) % 7
            day_start = today_midnight + timedelta(days=day_offset)

            for period_weekday, start_minutes, _end_minutes, _start_time, _end_time in parsed_blackouts:
                if period_weekday != target_day:
                    continue

                start_dt = day_start + timedelta(minutes=start_minutes)
                if start_dt <= now:
                    continue

                minutes_until = int((start_dt - now).total_seconds() // 60)
                if minutes_until < 0:
                    continue
                if best_minutes is None or minutes_until < best_minutes:
                    best_minutes = minutes_until

        return best_minutes

    @staticmethod
    def _merged_minutes(ranges: list[tuple[int, int]]) -> int:
        if not ranges:
            return 0
        sorted_ranges = sorted(ranges)
        merged_start, merged_end = sorted_ranges[0]
        total = 0
        for start, end in sorted_ranges[1:]:
            if start <= merged_end:
                merged_end = max(merged_end, end)
                continue
            total += max(0, merged_end - merged_start)
            merged_start, merged_end = start, end
        total += max(0, merged_end - merged_start)
        return total

    def _clipped_blackout_ranges(
        self,
        weekday: int,
        window_start_minutes: int,
        window_end_minutes: int,
        profile: RuleProfile,
    ) -> list[tuple[int, int]]:
        clipped: list[tuple[int, int]] = []
        for period_weekday, start_minutes, end_minutes, _start_time, _end_time in self._parsed_blackout_periods(profile):
            if period_weekday != weekday:
                continue
            clipped_start = max(window_start_minutes, start_minutes)
            clipped_end = min(window_end_minutes, end_minutes)
            if clipped_start < clipped_end:
                clipped.append((clipped_start, clipped_end))
        return clipped

    def _remaining_playable_window(self, now: datetime, profile: RuleProfile) -> tuple[int, int]:
        """Return (playable_days_remaining, non_blackout_minutes_remaining) until next week."""
        days_until_monday = (7 - now.weekday()) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        end_of_window = (now + timedelta(days=days_until_monday)).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        playable_days_remaining = 0
        non_blackout_minutes_remaining = 0

        while day_start < end_of_window:
            next_day = day_start + timedelta(days=1)
            segment_start = max(now, day_start)
            segment_end = min(end_of_window, next_day)
            if segment_start >= segment_end:
                day_start = next_day
                continue

            segment_start_minutes = int((segment_start - day_start).total_seconds() // 60)
            segment_end_minutes = int((segment_end - day_start).total_seconds() // 60)
            weekday = day_start.weekday()

            blackout_ranges = self._clipped_blackout_ranges(
                weekday=weekday,
                window_start_minutes=segment_start_minutes,
                window_end_minutes=segment_end_minutes,
                profile=profile,
            )

            segment_total = int((segment_end - segment_start).total_seconds() // 60)
            segment_blackout = self._merged_minutes(blackout_ranges)
            playable_minutes = max(0, segment_total - segment_blackout)

            non_blackout_minutes_remaining += playable_minutes
            if playable_minutes > 0:
                playable_days_remaining += 1

            day_start = next_day

        return playable_days_remaining, non_blackout_minutes_remaining

    def _this_week_non_blackout_minutes(self, now: datetime, profile: RuleProfile) -> int:
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        week_end = week_start + timedelta(days=7)

        non_blackout_minutes = 0
        day_start = week_start
        while day_start < week_end:
            next_day = day_start + timedelta(days=1)
            weekday = day_start.weekday()
            blackout_ranges = self._clipped_blackout_ranges(
                weekday=weekday,
                window_start_minutes=0,
                window_end_minutes=24 * 60,
                profile=profile,
            )

            blackout_minutes = self._merged_minutes(blackout_ranges)
            non_blackout_minutes += max(0, 24 * 60 - blackout_minutes)
            day_start = next_day

        return non_blackout_minutes

    @staticmethod
    def _effective_break_load(consumed_break_debt: int, active_remaining: int) -> int:
        """Return break guard load = consumed debt + still-active remaining minutes."""
        return max(0, consumed_break_debt) + max(0, active_remaining)

    def _load_runtime_state(self, now: datetime, profile: RuleProfile) -> RuntimeState:
        self._update_break_balance(now, profile)
        active = self._store.get_active_session(self._child_id)
        if active:
            _session_id, start_time, minutes_granted = active
            elapsed = self._elapsed_minutes(start_time, now)
            active_remaining = max(0, minutes_granted - elapsed)
        else:
            active_remaining = 0
        bank_balance = self._store.get_bank_balance(self._child_id)
        weekly_bank_baseline = self._weekly_bank_baseline(now, bank_balance)
        consumed_break_debt, _, rest_accumulated = self._store.get_consumed_break_debt(self._child_id)
        return RuntimeState(
            bank_balance=bank_balance,
            weekly_bank_baseline=weekly_bank_baseline,
            consumed_break_debt=consumed_break_debt,
            rest_accumulated=rest_accumulated,
            active_remaining=active_remaining,
            active_session=active,
            effective_break_load=self._effective_break_load(consumed_break_debt, active_remaining),
        )

    def _max_target_minutes_from_now(
        self,
        now: datetime,
        active_remaining: int,
        bank_balance: int,
        effective_break_load: int,
        profile: RuleProfile,
        minutes_until_blackout: int | None = None,
    ) -> int:
        """Return max request target (minutes from now) after all caps."""
        break_balance_room = profile.break_balance_max_minutes - effective_break_load
        additional_capacity = min(bank_balance, break_balance_room)
        max_target_from_now = active_remaining + max(0, additional_capacity)

        next_blackout = minutes_until_blackout
        if next_blackout is None:
            next_blackout = self._minutes_until_next_blackout(now, profile)
        if next_blackout is not None:
            max_target_from_now = min(max_target_from_now, next_blackout)

        return max(0, max_target_from_now)

    def _partial_grant_reason(
        self,
        requested_minutes: int,
        granted_minutes: int,
        state: RuntimeState,
        profile: RuleProfile,
        bank_capacity: int,
        break_capacity: int,
        blackout_capacity: int | None,
    ) -> str:
        if granted_minutes >= requested_minutes:
            return ""

        lines = [
            self._i18n.msg(
                "rules.partial_grant_summary",
                requested=format_duration(requested_minutes),
                granted=format_duration(granted_minutes),
            )
        ]

        if requested_minutes > bank_capacity:
            lines.append(
                self._i18n.msg(
                    "rules.partial_grant_reason_bank",
                    available=format_duration(bank_capacity),
                )
            )

        if requested_minutes > break_capacity:
            lines.append(
                self._i18n.msg(
                    "rules.partial_grant_reason_break_balance",
                    available=format_duration(break_capacity),
                    break_balance=format_duration(state.effective_break_load),
                    break_max=format_duration(profile.break_balance_max_minutes),
                )
            )

        if blackout_capacity is not None and requested_minutes > blackout_capacity:
            lines.append(
                self._i18n.msg(
                    "rules.partial_grant_reason_blackout",
                    available=format_duration(blackout_capacity),
                )
            )

        return "\n".join(lines)

    def evaluate_request(self, requested_minutes: int) -> PlaytimeDecision:
        """Evaluate a playtime request against all rules.
        
        Returns:
            PlaytimeDecision with allowed=True if request can be granted,
            allowed=False with reason if denied.
        """
        now = self._get_local_now()
        profile = self._profile()
        
        # 1. Check if in blackout period
        is_blackout, blackout_reason = self._is_in_blackout_period(now, profile)
        if is_blackout:
            return PlaytimeDecision(
                allowed=False,
                reason=blackout_reason,
            )
        
        state = self._load_runtime_state(now, profile)

        if state.effective_break_load >= profile.break_balance_max_minutes:
            recovery_time_needed = self._recovery_minutes(state.effective_break_load, profile)
            return PlaytimeDecision(
                allowed=False,
                reason=(
                    self._i18n.msg(
                        "rules.recovery_maxed_denied",
                        recovery_balance=format_duration(state.effective_break_load),
                        recovery_time=format_duration(recovery_time_needed),
                    )
                ),
            )
        
        if state.bank_balance <= 0 and requested_minutes > state.active_remaining:
            return PlaytimeDecision(
                allowed=False,
                reason=self._i18n.msg("rules.bank_empty_denied"),
            )

        # 4.5 If request is below current active remaining, this is a no-op.
        # Deny with a dedicated warning so caller can inform user and skip upstream API call.
        if state.active_remaining > 0 and requested_minutes <= state.active_remaining:
            return PlaytimeDecision(
                allowed=False,
                reason=self._i18n.msg(
                    "rules.request_below_active_remaining",
                    requested=format_duration(requested_minutes),
                    remaining=format_duration(state.active_remaining),
                ),
            )
        
        bank_capacity = state.active_remaining + max(0, state.bank_balance)
        break_capacity = state.active_remaining + max(0, profile.break_balance_max_minutes - state.effective_break_load)
        blackout_capacity = self._minutes_until_next_blackout(now, profile)
        minutes_to_grant = min(
            max(0, requested_minutes),
            self._max_target_minutes_from_now(
                now=now,
                active_remaining=state.active_remaining,
                bank_balance=state.bank_balance,
                effective_break_load=state.effective_break_load,
                profile=profile,
                minutes_until_blackout=blackout_capacity,
            ),
        )

        cannot_grant_now_reason = self._i18n.msg(
            "rules.cannot_grant_now",
            bank=format_duration(state.bank_balance),
            break_balance=format_duration(state.effective_break_load),
            break_max=format_duration(profile.break_balance_max_minutes),
        )
        if minutes_to_grant <= max(0, state.active_remaining):
            return PlaytimeDecision(
                allowed=False,
                reason=cannot_grant_now_reason,
            )
        
        return PlaytimeDecision(
            allowed=True,
            reason=self._partial_grant_reason(
                requested_minutes=requested_minutes,
                granted_minutes=minutes_to_grant,
                state=state,
                profile=profile,
                bank_capacity=bank_capacity,
                break_capacity=break_capacity,
                blackout_capacity=blackout_capacity,
            ),
            minutes_granted=minutes_to_grant,
        )

    def grant_playtime(self, minutes: int) -> tuple[int, str]:
        """Grant playtime and update usage tracking.
        
        Returns (session_id, message).
        """
        now = self._get_local_now()
        profile = self._profile()

        state = self._load_runtime_state(now, profile)
        additional_minutes = max(0, minutes - state.active_remaining)

        # Deduct only additional time beyond current remaining session time.
        new_bank = state.bank_balance - additional_minutes
        self._store.set_bank_balance(self._child_id, new_bank)
        consumed_break_debt = state.consumed_break_debt

        if state.active_session:
            session_id, _start_time, current_granted = state.active_session
            new_total_granted = current_granted + additional_minutes
            self._store.set_session_minutes_granted(session_id, new_total_granted)
        else:
            session_id = self._store.add_session(self._child_id, now, minutes)

        lines = [self._i18n.msg("rules.grant_confirm", minutes=format_duration(minutes))]
        if state.active_session:
            lines.append(self._i18n.msg("rules.added_now", minutes=format_duration(additional_minutes)))
        lines.extend(
            [
                self._i18n.msg(
                    "rules.bank_remaining",
                    minutes=format_duration(new_bank),
                ),
                self._bank_pace_line(new_bank, state.weekly_bank_baseline, now, profile),
                self._i18n.msg(
                    "rules.break_balance",
                    balance=format_duration(consumed_break_debt),
                    max_balance=format_duration(profile.break_balance_max_minutes),
                ),
            ]
        )

        return (session_id, "\n".join(lines))

    def complete_session(self) -> str:
        """Complete the active session.

        Returns status message.
        """
        now = self._get_local_now()
        profile = self._profile()

        # Refresh break debt first: this can also auto-complete naturally expired sessions.
        self._update_break_balance(now, profile)
        active = self._store.get_active_session(self._child_id)
        
        if not active:
            return self._i18n.msg("rules.no_active_session")
        
        session_id, start_time, minutes_granted = active
        
        # Calculate actual time used if ended early
        elapsed_minutes = self._elapsed_minutes(start_time, now)
        actual_minutes = min(elapsed_minutes, minutes_granted)
        ended_early = actual_minutes < minutes_granted

        # If ended early, credit the unused time back to bank
        if ended_early:
            unused = minutes_granted - actual_minutes
            bank_balance = self._store.get_bank_balance(self._child_id)
            self._store.set_bank_balance(self._child_id, bank_balance + unused)
        
        # Mark session complete
        self._store.complete_session(session_id, now)
        
        message = self._i18n.msg("rules.session_completed", minutes=format_duration(actual_minutes))
        if ended_early:
            message += self._i18n.msg("rules.session_completed_credited", minutes=format_duration(unused))
        
        return message

    def has_active_session(self) -> bool:
        self._update_break_balance(self._get_local_now(), self._profile())
        return self._store.get_active_session(self._child_id) is not None

    def get_status(self) -> str:
        """Get current playtime status."""
        now = self._get_local_now()
        profile = self._profile()

        state = self._load_runtime_state(now, profile)
        lines = [
            self._i18n.msg("rules.status_title"),
            "",
            self._i18n.msg(
                "rules.status_bank_total_max",
                total=format_duration(state.bank_balance),
                max_balance=format_duration(profile.max_bank_minutes),
            ),
            self._bank_pace_line(state.bank_balance, state.weekly_bank_baseline, now, profile),
            self._i18n.msg(
                "rules.status_break_balance",
                balance=format_duration(state.consumed_break_debt),
                max_balance=format_duration(profile.break_balance_max_minutes),
            ),
            "",
        ]

        # Calculate time available from now (active session included if present).
        minutes_until_blackout = self._minutes_until_next_blackout(now, profile)
        max_target_from_now = self._max_target_minutes_from_now(
            now=now,
            active_remaining=state.active_remaining,
            bank_balance=state.bank_balance,
            effective_break_load=state.effective_break_load,
            profile=profile,
            minutes_until_blackout=minutes_until_blackout,
        )
        weekday = now.weekday()
        current_minutes = now.hour * 60 + now.minute
        periods_with_start: list[tuple[int, str]] = []
        for period_weekday, start_minutes, end_minutes, start_time, end_time in self._parsed_blackout_periods(profile):
            if period_weekday != weekday or end_minutes <= current_minutes:
                continue
            periods_with_start.append((start_minutes, f"{start_time}-{end_time}"))
        periods_with_start.sort(key=lambda item: item[0])
        today_blackout_periods = [period for _start, period in periods_with_start]

        # Blackout check
        is_blackout, _blackout_reason = self._is_in_blackout_period(now, profile)
        if is_blackout:
            max_target_from_now = state.active_remaining if state.active_session else 0
        if today_blackout_periods:
            lines.append(
                self._i18n.msg(
                    "rules.status_today_current_and_upcoming_blackouts",
                    periods=", ".join(today_blackout_periods),
                )
            )
            lines.append("")
        if state.active_session:
            lines.append(self._i18n.msg("rules.status_active_session", minutes=format_duration(state.active_remaining)))
            additional_minutes = max(0, max_target_from_now - state.active_remaining)
            lines.append(
                self._i18n.msg(
                    "rules.status_can_request_additional",
                    minutes=format_duration(additional_minutes),
                )
            )
            lines.append("")
        else:
            lines.append(self._i18n.msg("rules.status_can_play_now", minutes=format_duration(max_target_from_now)))
            lines.append("")
        
        # If a break starts now, only already-consumed break debt needs recovery.
        recovery_basis = state.consumed_break_debt
        if recovery_basis > 0:
            recovery_time = self._recovery_minutes(recovery_basis, profile)
            lines.append(self._i18n.msg("rules.status_recovery_rate", rate=profile.break_recovery_rate))
            if state.active_session:
                lines.append(
                    self._i18n.msg(
                        "rules.status_recovery_needed_active",
                        minutes=format_duration(recovery_time),
                    )
                )
            else:
                lines.append(self._i18n.msg("rules.status_recovery_needed", minutes=format_duration(recovery_time)))
                if state.rest_accumulated > 0:
                    lines.append(
                        self._i18n.msg(
                            "rules.status_recovery_progress",
                            current=format_duration(int(state.rest_accumulated)),
                            needed=format_duration(recovery_time),
                        )
                    )
            lines.append("")
        
        return "\n".join(lines)

    def add_to_bank(self, minutes: int) -> str:
        """Add minutes to the bank (admin function)."""
        now = self._get_local_now()
        profile = self._profile()
        actual_added = self._store.add_to_bank(self._child_id, minutes, profile.max_bank_minutes)
        bank_balance = self._store.get_bank_balance(self._child_id)
        if actual_added > 0:
            self._store.add_to_weekly_bank_baseline(
                self._child_id,
                self._current_week_start_iso(now),
                actual_added,
                bank_balance - actual_added,
            )
        return self._i18n.msg(
            "rules.add_to_bank",
            actual_added=format_duration(actual_added),
            bank=format_duration(bank_balance),
            max_bank=format_duration(profile.max_bank_minutes),
        )

    def set_bank(self, minutes: int) -> str:
        """Set bank to an explicit value in minutes (admin function)."""
        now = self._get_local_now()
        profile = self._profile()
        capped_minutes = max(0, min(minutes, profile.max_bank_minutes))
        old_balance = self._store.get_bank_balance(self._child_id)
        self._store.set_bank_balance(self._child_id, capped_minutes)
        increase = capped_minutes - old_balance
        if increase > 0:
            self._store.add_to_weekly_bank_baseline(
                self._child_id,
                self._current_week_start_iso(now),
                increase,
                old_balance,
            )

        message = self._i18n.msg(
            "rules.set_bank",
            old_balance=format_duration(old_balance),
            new_balance=format_duration(capped_minutes),
            max_bank=format_duration(profile.max_bank_minutes),
        )
        if capped_minutes != minutes:
            message += self._i18n.msg("rules.set_bank_capped")
        return message

    def modify_bank(self, delta_minutes: int) -> str:
        """Adjust bank by a relative delta in minutes (admin function)."""
        current_balance = self._store.get_bank_balance(self._child_id)
        return self.set_bank(current_balance + delta_minutes)

    def set_consumed_break_debt(self, minutes: int) -> str:
        """Set consumed break debt to an explicit value in minutes (admin function)."""
        profile = self._profile()
        capped_minutes = min(minutes, profile.break_balance_max_minutes)
        old_balance, _, _rest = self._store.get_consumed_break_debt(self._child_id)
        self._store.set_consumed_break_debt(self._child_id, capped_minutes, self._get_local_now(), 0.0)

        message = self._i18n.msg(
            "rules.set_break_balance",
            old_balance=format_duration(old_balance),
            new_balance=format_duration(capped_minutes),
            max_balance=format_duration(profile.break_balance_max_minutes),
        )
        if capped_minutes != minutes:
            message += self._i18n.msg("rules.set_break_balance_capped")
        return message

    def rollover_week(self) -> str:
        """Handle weekly rollover: add weekly allowance to bank (up to limit)."""
        now = self._get_local_now()
        profile = self._profile()
        bank_balance = self._store.get_bank_balance(self._child_id)
        new_bank = min(
            bank_balance + profile.weekly_addition_minutes,
            profile.max_bank_minutes
        )
        actual_added = new_bank - bank_balance
        
        self._store.set_bank_balance(self._child_id, new_bank)
        self._store.set_weekly_bank_baseline(
            self._child_id,
            self._current_week_start_iso(now),
            new_bank,
        )
        
        return self._i18n.msg(
            "rules.rollover_week",
            actual_added=format_duration(actual_added),
            max_bank=format_duration(profile.max_bank_minutes),
            new_bank=format_duration(new_bank),
        )
