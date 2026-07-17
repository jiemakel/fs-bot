from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Callable

from dateutil import tz

from family_safety_bot.config import RuleProfile, Settings, parse_clock_minutes
from family_safety_bot.formatting import format_duration
from family_safety_bot.i18n import I18n
from family_safety_bot.storage import ActiveSession, PlaytimeStore


def _parse_blackout_period(weekday: int, start_time: str, end_time: str) -> tuple[int, int, int, str, str] | None:
    if (start_minutes := parse_clock_minutes(start_time)) is None or (end_minutes := parse_clock_minutes(end_time)) is None:
        return None
    return (weekday, start_minutes, end_minutes, start_time, end_time)


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
    weekly_playtime_used: int
    weekly_playtime_remaining: int
    accrued_playtime: int
    rest_accumulated: float
    active_remaining: int
    active_session: ActiveSession | None
    effective_playtime_load: int


@dataclass(frozen=True)
class RecoveryCompletion:
    recovery_minutes: int
    accrued_playtime_minutes: int


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
    def _parse_blackout_periods(
        blackout_periods: tuple[tuple[int, str, str], ...],
    ) -> tuple[tuple[int, int, int, str, str], ...]:
        return tuple(
            result
            for weekday, start_time, end_time in blackout_periods
            if (result := _parse_blackout_period(weekday, start_time, end_time)) is not None
        )

    def _parsed_blackout_periods(self, profile: RuleProfile) -> tuple[tuple[int, int, int, str, str], ...]:
        return self._parse_blackout_periods(tuple(profile.blackout_periods))

    def _get_local_now(self) -> datetime:
        """Get current time in configured timezone."""
        return datetime.now(self._tz)

    @staticmethod
    def _elapsed_minutes(start_time: datetime, now: datetime) -> int:
        return max(0, int((now - start_time).total_seconds() / 60))

    def _recovery_minutes(self, playtime_minutes: int, profile: RuleProfile) -> int:
        return max(0, math.ceil((playtime_minutes / profile.break_recovery_rate) - 1e-9))

    @staticmethod
    def _active_play_overlap(active: ActiveSession, last_update: datetime, now: datetime) -> tuple[int, datetime]:
        _session_id, start_time, minutes_granted = active
        session_end = start_time + timedelta(minutes=minutes_granted)
        play_end = min(now, session_end)
        overlap_sec = (play_end - max(last_update, start_time)).total_seconds()
        return (max(0, int(overlap_sec / 60)), play_end)

    def _current_week_start(self, now: datetime) -> datetime:
        return (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    def _weekly_playtime_used(self, now: datetime) -> int:
        week_start = self._current_week_start(now)
        return self._store.get_playtime_minutes_in_window(
            self._child_id,
            week_start,
            week_start + timedelta(days=7),
        )

    def _weekly_allowance_line(self, used: int, profile: RuleProfile) -> str:
        return self._i18n.msg(
            "rules.weekly_allowance",
            used=format_duration(used),
            remaining=format_duration(max(0, profile.weekly_max_minutes - used)),
            maximum=format_duration(profile.weekly_max_minutes),
        )

    def _weekly_allowance_pace_metrics(
        self,
        weekly_remaining_minutes: int,
        now: datetime,
        profile: RuleProfile,
    ) -> tuple[int, float, float, float]:
        playable_days, non_blackout_remaining = self._remaining_playable_window(now, profile)
        avg_per_day = int(round(weekly_remaining_minutes / playable_days)) if playable_days > 0 else 0

        week_non_blackout = self._this_week_non_blackout_minutes(now, profile)
        allowance_share = (
            weekly_remaining_minutes / profile.weekly_max_minutes
            if profile.weekly_max_minutes > 0
            else 0.0
        )
        non_blackout_share = (non_blackout_remaining / week_non_blackout) if week_non_blackout > 0 else 0.0
        pace_ratio = (allowance_share / non_blackout_share) if non_blackout_share > 0 else 0.0
        return avg_per_day, allowance_share, non_blackout_share, pace_ratio

    def _weekly_allowance_pace_line(
        self,
        weekly_remaining_minutes: int,
        now: datetime,
        profile: RuleProfile,
    ) -> str:
        avg_per_day, allowance_share, non_blackout_share, pace_ratio = self._weekly_allowance_pace_metrics(
            weekly_remaining_minutes,
            now,
            profile,
        )
        return self._i18n.msg(
            "rules.status_weekly_allowance_avg_pace",
            avg_per_day=format_duration(avg_per_day),
            allowance_share=f"{allowance_share * 100:.1f}%",
            non_blackout_share=f"{non_blackout_share * 100:.1f}%",
            pace_ratio=f"{pace_ratio:.2f}x",
        )

    def _update_accrued_playtime(
        self,
        now: datetime,
        profile: RuleProfile,
        *,
        accrue_active: bool = True,
    ) -> RecoveryCompletion | None:
        """Update accrued playtime and return completion details if recovery completed.

        Accrued playtime tracks consumed playtime:
        - increases during active playtime minutes
        - decreases only after a complete recovery period (rest_accumulated >= playtime / break_recovery_rate)
        - any new play resets the rest accumulation counter
        """
        accrued_playtime, last_update, rest_accumulated = self._store.get_accrued_playtime(self._child_id)
        if (time_passed := (now - last_update).total_seconds() / 60) <= 0:
            return None

        play_minutes = 0.0
        play_end = last_update
        if active := self._store.get_active_session(self._child_id):
            session_id, start_time, minutes_granted = active
            session_end = start_time + timedelta(minutes=minutes_granted)
            if not accrue_active and now < session_end:
                return None
            if now >= session_end:
                # Expired sessions complete at their actual end, not at the later check time.
                self._store.complete_session(session_id, session_end)
            # Count only the overlap between the elapsed window and the session.
            play_minutes, play_end = self._active_play_overlap(active, last_update, now)

        if play_minutes > 0:
            # New play resets rest accumulation; post-session rest starts fresh.
            new_debt = accrued_playtime + play_minutes
            new_rest = (now - play_end).total_seconds() / 60 if play_end < now else 0.0
        else:
            new_debt = accrued_playtime
            new_rest = rest_accumulated + time_passed if new_debt > 0 else 0.0

        recovery_completion = None
        # Recovery is granted only after the full rest period completes.
        if new_debt > 0 and (recovery_needed := new_debt / profile.break_recovery_rate) <= new_rest:
            recovery_completion = RecoveryCompletion(
                self._recovery_minutes(new_debt, profile),
                new_debt,
            )
            new_debt = new_rest = 0.0

        self._store.set_accrued_playtime(self._child_id, new_debt, now, new_rest)
        return recovery_completion

    def _is_in_blackout_period(self, dt: datetime, profile: RuleProfile | None = None) -> tuple[bool, str]:
        """Check if given time is in a blackout period. Returns (is_blackout, reason)."""
        weekday = dt.weekday()
        current_minutes = dt.hour * 60 + dt.minute
        for period_weekday, start_minutes, end_minutes, start_time, end_time in self._parsed_blackout_periods(
            profile or self._profile()
        ):
            if weekday == period_weekday and start_minutes <= current_minutes < end_minutes:
                return True, self._i18n.msg("rules.blackout_denied", start_time=start_time, end_time=end_time)
        return False, ""

    def _minutes_until_next_blackout(self, dt: datetime, profile: RuleProfile | None = None) -> int | None:
        """Return minutes until next blackout starts, or None if no blackout periods exist."""
        parsed_blackouts = self._parsed_blackout_periods(profile or self._profile())
        if not parsed_blackouts:
            return None

        today_midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        best: int | None = None
        for day_offset in range(8):
            target_day = (dt.weekday() + day_offset) % 7
            day_start = today_midnight + timedelta(days=day_offset)
            for period_weekday, start_minutes, _, _, _ in parsed_blackouts:
                if period_weekday != target_day:
                    continue
                start_dt = day_start + timedelta(minutes=start_minutes)
                if start_dt > dt:
                    minutes_until = int((start_dt - dt).total_seconds() // 60)
                    if best is None or minutes_until < best:
                        best = minutes_until
        return best

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
        return [
            (max(window_start_minutes, start), min(window_end_minutes, end))
            for pw, start, end, _, _ in self._parsed_blackout_periods(profile)
            if pw == weekday and max(window_start_minutes, start) < min(window_end_minutes, end)
        ]

    def _non_blackout_minutes_for_window(
        self,
        weekday: int,
        start_minutes: int,
        end_minutes: int,
        profile: RuleProfile,
    ) -> int:
        blackout_minutes = self._merged_minutes(
            self._clipped_blackout_ranges(weekday, start_minutes, end_minutes, profile)
        )
        return max(0, end_minutes - start_minutes - blackout_minutes)

    def _remaining_playable_window(self, now: datetime, profile: RuleProfile) -> tuple[int, int]:
        """Return (playable_days_remaining, non_blackout_minutes_remaining) until next week."""
        days_until_monday = 7 - now.weekday()
        end_of_window = (now + timedelta(days=days_until_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
        today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        playable_days = 0
        non_blackout_remaining = 0
        day_start = today_midnight
        while day_start < end_of_window:
            start_min = now.hour * 60 + now.minute if day_start == today_midnight else 0
            playable = self._non_blackout_minutes_for_window(day_start.weekday(), start_min, 24 * 60, profile)
            non_blackout_remaining += playable
            if playable > 0:
                playable_days += 1
            day_start += timedelta(days=1)
        return playable_days, non_blackout_remaining

    def _this_week_non_blackout_minutes(self, now: datetime, profile: RuleProfile) -> int:
        total = 0
        for weekday in range(7):
            blackout_ranges = self._clipped_blackout_ranges(weekday, 0, 24 * 60, profile)
            total += 24 * 60 - self._merged_minutes(blackout_ranges)
        return total

    @staticmethod
    def _effective_playtime_load(accrued_playtime: int, active_remaining: int) -> int:
        """Return playtime guard load = accrued playtime + still-active remaining minutes."""
        return max(0, accrued_playtime) + max(0, active_remaining)

    def _load_runtime_state(self, now: datetime, profile: RuleProfile) -> RuntimeState:
        self._update_accrued_playtime(now, profile, accrue_active=False)
        active = self._store.get_active_session(self._child_id)
        if active:
            _session_id, start_time, minutes_granted = active
            active_remaining = max(0, minutes_granted - self._elapsed_minutes(start_time, now))
        else:
            active_remaining = 0
        bank_balance = self._store.get_bank_balance(self._child_id)
        weekly_playtime_used = self._weekly_playtime_used(now)
        accrued_playtime, last_update, rest_accumulated = self._store.get_accrued_playtime(self._child_id)
        if active:
            # Include active elapsed time for decisions/status without making scheduler cadence affect stored debt.
            accrued_playtime += self._active_play_overlap(active, last_update, now)[0]
            rest_accumulated = 0.0
        return RuntimeState(
            bank_balance=bank_balance,
            weekly_playtime_used=weekly_playtime_used,
            weekly_playtime_remaining=max(0, profile.weekly_max_minutes - weekly_playtime_used),
            accrued_playtime=accrued_playtime,
            rest_accumulated=rest_accumulated,
            active_remaining=active_remaining,
            active_session=active,
            effective_playtime_load=self._effective_playtime_load(accrued_playtime, active_remaining),
        )

    def _max_target_minutes_from_now(
        self,
        active_remaining: int,
        bank_balance: int,
        effective_playtime_load: int,
        weekly_playtime_remaining: int,
        profile: RuleProfile,
        minutes_until_blackout: int | None,
    ) -> int:
        """Return max request target (minutes from now) after all caps."""
        playtime_room = profile.accrued_playtime_max_minutes - effective_playtime_load
        max_target_from_now = active_remaining + max(
            0,
            min(bank_balance, playtime_room, weekly_playtime_remaining),
        )
        if minutes_until_blackout is not None:
            max_target_from_now = min(max_target_from_now, minutes_until_blackout)
        return max(0, max_target_from_now)

    def _partial_grant_reason(
        self,
        requested_minutes: int,
        granted_minutes: int,
        state: RuntimeState,
        profile: RuleProfile,
        bank_capacity: int,
        weekly_capacity: int,
        accrued_playtime_capacity: int,
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

        if requested_minutes > weekly_capacity:
            lines.append(
                self._i18n.msg(
                    "rules.partial_grant_reason_weekly",
                    available=format_duration(weekly_capacity),
                    used=format_duration(state.weekly_playtime_used),
                    maximum=format_duration(profile.weekly_max_minutes),
                )
            )

        if requested_minutes > accrued_playtime_capacity:
            lines.append(
                self._i18n.msg(
                    "rules.partial_grant_reason_accrued_playtime",
                    available=format_duration(accrued_playtime_capacity),
                    accrued_playtime=format_duration(state.effective_playtime_load),
                    accrued_max=format_duration(profile.accrued_playtime_max_minutes),
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

        if state.effective_playtime_load >= profile.accrued_playtime_max_minutes:
            recovery_time_needed = self._recovery_minutes(state.effective_playtime_load, profile)
            return PlaytimeDecision(
                allowed=False,
                reason=(
                    self._i18n.msg(
                        "rules.recovery_maxed_denied",
                        accrued_playtime=format_duration(state.effective_playtime_load),
                        recovery_time=format_duration(recovery_time_needed),
                    )
                ),
            )
        
        if state.bank_balance <= 0 and requested_minutes > state.active_remaining:
            return PlaytimeDecision(
                allowed=False,
                reason=self._i18n.msg("rules.bank_empty_denied"),
            )

        if state.weekly_playtime_remaining <= 0 and requested_minutes > state.active_remaining:
            return PlaytimeDecision(
                allowed=False,
                reason=self._i18n.msg(
                    "rules.weekly_maxed_denied",
                    used=format_duration(state.weekly_playtime_used),
                    maximum=format_duration(profile.weekly_max_minutes),
                ),
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
        weekly_capacity = state.active_remaining + state.weekly_playtime_remaining
        accrued_playtime_capacity = state.active_remaining + max(0, profile.accrued_playtime_max_minutes - state.effective_playtime_load)
        blackout_capacity = self._minutes_until_next_blackout(now, profile)
        minutes_to_grant = min(
            max(0, requested_minutes),
            self._max_target_minutes_from_now(
                active_remaining=state.active_remaining,
                bank_balance=state.bank_balance,
                effective_playtime_load=state.effective_playtime_load,
                weekly_playtime_remaining=state.weekly_playtime_remaining,
                profile=profile,
                minutes_until_blackout=blackout_capacity,
            ),
        )

        cannot_grant_now_reason = self._i18n.msg(
            "rules.cannot_grant_now",
            bank=format_duration(state.bank_balance),
            weekly_used=format_duration(state.weekly_playtime_used),
            weekly_max=format_duration(profile.weekly_max_minutes),
            accrued_playtime=format_duration(state.effective_playtime_load),
            accrued_max=format_duration(profile.accrued_playtime_max_minutes),
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
                weekly_capacity=weekly_capacity,
                accrued_playtime_capacity=accrued_playtime_capacity,
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
        accrued_playtime = state.accrued_playtime

        # Detect recovery abort: starting a NEW session while mid-recovery.
        recovery_aborted = (
            state.active_session is None
            and state.accrued_playtime > 0
            and state.rest_accumulated > 0
        )
        if recovery_aborted:
            self._store.set_recovery_abort(self._child_id, state.rest_accumulated, now)
        else:
            self._store.clear_recovery_abort(self._child_id)

        if state.active_session is not None:
            session_id, _start_time, current_granted = state.active_session
            new_total_granted = current_granted + additional_minutes
            self._store.set_session_minutes_granted(session_id, new_total_granted)
        else:
            self._store.set_accrued_playtime(self._child_id, state.accrued_playtime, now, 0.0)
            session_id = self._store.add_session(self._child_id, now, minutes)

        lines = [self._i18n.msg("rules.grant_confirm", minutes=format_duration(minutes))]
        if state.active_session is not None:
            lines.append(self._i18n.msg("rules.added_now", minutes=format_duration(additional_minutes)))
        lines.extend(
            [
                self._i18n.msg(
                    "rules.bank_remaining",
                    minutes=format_duration(new_bank),
                ),
                self._weekly_allowance_line(self._weekly_playtime_used(now), profile),
                self._weekly_allowance_pace_line(
                    max(0, profile.weekly_max_minutes - self._weekly_playtime_used(now)),
                    now,
                    profile,
                ),
                self._i18n.msg(
                    "rules.accrued_playtime",
                    balance=format_duration(accrued_playtime),
                    max_balance=format_duration(profile.accrued_playtime_max_minutes),
                ),
            ]
        )
        if recovery_aborted:
            recovery_needed = self._recovery_minutes(accrued_playtime, profile)
            lines.append(
                self._i18n.msg(
                    "rules.recovery_aborted_warning",
                    rest_done=format_duration(int(state.rest_accumulated)),
                    rest_needed=format_duration(recovery_needed),
                )
            )

        return (session_id, "\n".join(lines))

    def complete_session(self) -> str:
        """Complete the active session.

        Returns status message.
        """
        now = self._get_local_now()
        profile = self._profile()

        # Refresh accrued playtime first: this can also auto-complete naturally expired sessions.
        self._update_accrued_playtime(now, profile)
        active = self._store.get_active_session(self._child_id)
        
        if active is None:
            return self._i18n.msg("rules.no_active_session")
        
        session_id, start_time, minutes_granted = active
        
        # Calculate actual time used if ended early
        elapsed_minutes = self._elapsed_minutes(start_time, now)
        actual_minutes = min(elapsed_minutes, minutes_granted)
        ended_early = actual_minutes < minutes_granted
        unused = 0

        # If ended early, credit the unused time back to bank
        if ended_early:
            unused = minutes_granted - actual_minutes
            bank_balance = self._store.get_bank_balance(self._child_id)
            self._store.set_bank_balance(self._child_id, bank_balance + unused)
        
        # Mark session complete
        self._store.complete_session(session_id, now)

        # Check if this stop is within the 1-minute grace window after a recovery abort.
        restored_rest_accumulated: float | None = None
        abort = self._store.get_recovery_abort(self._child_id)
        if abort is not None:
            saved_rest, abort_time = abort
            seconds_since_abort = (now - abort_time).total_seconds()
            if 0 <= seconds_since_abort <= 60:
                # Restore saved rest_accumulated so recovery continues as if uninterrupted.
                current_playtime, _, _ = self._store.get_accrued_playtime(self._child_id)
                restored_playtime = current_playtime - int(seconds_since_abort / 60)
                self._store.set_accrued_playtime(self._child_id, restored_playtime, now, saved_rest)
                restored_rest_accumulated = saved_rest
            self._store.clear_recovery_abort(self._child_id)
        
        message = self._i18n.msg("rules.session_completed", minutes=format_duration(actual_minutes))
        if ended_early:
            message += self._i18n.msg("rules.session_completed_credited", minutes=format_duration(unused))
        if restored_rest_accumulated is not None:
            message += "\n" + self._i18n.msg(
                "rules.recovery_restored",
                rest_done=format_duration(int(restored_rest_accumulated)),
            )
        message += "\n" + self._weekly_allowance_line(self._weekly_playtime_used(now), profile)
        
        return message

    def has_active_session(self) -> bool:
        self._update_accrued_playtime(self._get_local_now(), self._profile(), accrue_active=False)
        return self._store.get_active_session(self._child_id) is not None

    def check_recovery_completion(self) -> str | None:
        """Update recovery state and return a one-shot completion message if recovery finished."""
        recovery = self._update_accrued_playtime(self._get_local_now(), self._profile(), accrue_active=False)
        if recovery is None:
            return None
        return self._i18n.msg(
            "rules.recovery_completed",
            recovery=format_duration(recovery.recovery_minutes),
            accrued_playtime=format_duration(recovery.accrued_playtime_minutes),
        )

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
            self._weekly_allowance_line(state.weekly_playtime_used, profile),
            self._weekly_allowance_pace_line(state.weekly_playtime_remaining, now, profile),
            self._i18n.msg(
                "rules.status_accrued_playtime",
                balance=format_duration(state.accrued_playtime),
                max_balance=format_duration(profile.accrued_playtime_max_minutes),
            ),
            "",
        ]

        # Calculate time available from now (active session included if present).
        minutes_until_blackout = self._minutes_until_next_blackout(now, profile)
        max_target_from_now = self._max_target_minutes_from_now(
            active_remaining=state.active_remaining,
            bank_balance=state.bank_balance,
            effective_playtime_load=state.effective_playtime_load,
            weekly_playtime_remaining=state.weekly_playtime_remaining,
            profile=profile,
            minutes_until_blackout=minutes_until_blackout,
        )
        weekday = now.weekday()
        current_minutes = now.hour * 60 + now.minute
        today_blackout_periods = [
            period
            for _start, period in sorted(
                (start_minutes, f"{start_time}-{end_time}")
                for period_weekday, start_minutes, end_minutes, start_time, end_time in self._parsed_blackout_periods(profile)
                if period_weekday == weekday and end_minutes > current_minutes
            )
        ]

        # Blackout check
        is_blackout, _blackout_reason = self._is_in_blackout_period(now, profile)
        if is_blackout:
            max_target_from_now = state.active_remaining if state.active_session is not None else 0
        if today_blackout_periods:
            lines.append(
                self._i18n.msg(
                    "rules.status_today_current_and_upcoming_blackouts",
                    periods=", ".join(today_blackout_periods),
                )
            )
            lines.append("")
        if state.active_session is not None:
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
        
        # If a break starts now, only already-consumed playtime needs recovery.
        recovery_basis = state.accrued_playtime
        if recovery_basis > 0:
            recovery_time = self._recovery_minutes(recovery_basis, profile)
            lines.append(self._i18n.msg("rules.status_recovery_rate", rate=profile.break_recovery_rate))
            if state.active_session is not None:
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
        return self._i18n.msg(
            "rules.add_to_bank",
            actual_added=format_duration(actual_added),
            bank=format_duration(bank_balance),
            max_bank=format_duration(profile.max_bank_minutes),
            weekly_remaining=format_duration(max(0, profile.weekly_max_minutes - self._weekly_playtime_used(now))),
            weekly_max=format_duration(profile.weekly_max_minutes),
        )

    def set_bank(self, minutes: int) -> str:
        """Set bank to an explicit value in minutes (admin function)."""
        now = self._get_local_now()
        profile = self._profile()
        capped_minutes = max(0, min(minutes, profile.max_bank_minutes))
        old_balance = self._store.get_bank_balance(self._child_id)
        self._store.set_bank_balance(self._child_id, capped_minutes)
        message = self._i18n.msg(
            "rules.set_bank",
            old_balance=format_duration(old_balance),
            new_balance=format_duration(capped_minutes),
            max_bank=format_duration(profile.max_bank_minutes),
            weekly_remaining=format_duration(max(0, profile.weekly_max_minutes - self._weekly_playtime_used(now))),
            weekly_max=format_duration(profile.weekly_max_minutes),
        )
        if capped_minutes != minutes:
            message += self._i18n.msg("rules.set_bank_capped")
        return message

    def modify_bank(self, delta_minutes: int) -> str:
        """Adjust bank by a relative delta in minutes (admin function)."""
        current_balance = self._store.get_bank_balance(self._child_id)
        return self.set_bank(current_balance + delta_minutes)

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
        return self._i18n.msg(
            "rules.rollover_week",
            actual_added=format_duration(actual_added),
            max_bank=format_duration(profile.max_bank_minutes),
            new_bank=format_duration(new_bank),
            weekly_remaining=format_duration(max(0, profile.weekly_max_minutes - self._weekly_playtime_used(now))),
            weekly_max=format_duration(profile.weekly_max_minutes),
        )
