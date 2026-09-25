from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Callable

from dateutil import tz

from family_safety_bot.config import (
    WEEKDAY_SUFFIXES,
    BankProfile,
    RuleProfile,
    Settings,
    parse_clock_minutes,
)
from family_safety_bot.formatting import format_duration
from family_safety_bot.i18n import I18n
from family_safety_bot.storage import ActiveSession, PlaytimeStore

RECOVERY_RESTORE_GRACE_PERIOD_SECONDS = 120


def _parse_blackout_period(
    weekday: int,
    start_time: str,
    end_time: str,
) -> tuple[int, int, int, str, str] | None:
    start_minutes = parse_clock_minutes(start_time)
    end_minutes = parse_clock_minutes(end_time)
    if start_minutes is None or end_minutes is None:
        return None
    return weekday, start_minutes, end_minutes, start_time, end_time


@dataclass
class PlaytimeDecision:
    allowed: bool
    reason: str
    minutes_granted: int = 0


@dataclass
class RuntimeState:
    bank_balances: dict[str, int]
    active_remaining: int
    active_session: ActiveSession | None


@dataclass
class AutomaticUpdate:
    recovered_banks: list[str]
    naturally_completed_minutes: int | None = None


class PlaytimeRules:
    """Enforce playtime rules and manage named banks for one child."""

    def __init__(
        self,
        child_id: str,
        settings: Settings,
        store: PlaytimeStore,
        profile_provider: Callable[[], RuleProfile],
        i18n: I18n | None = None,
    ) -> None:
        self._child_id = child_id
        self._store = store
        self._profile_provider = profile_provider
        self._tz = tz.gettz(settings.timezone)
        self._i18n = i18n or I18n("en")

    def _profile(self) -> RuleProfile:
        return self._profile_provider()

    @staticmethod
    def _parsed_blackout_periods(profile: RuleProfile) -> tuple[tuple[int, int, int, str, str], ...]:
        return tuple(
            parsed
            for weekday, start_time, end_time in profile.blackout_periods
            if (parsed := _parse_blackout_period(weekday, start_time, end_time)) is not None
        )

    def _get_local_now(self) -> datetime:
        return datetime.now(self._tz)

    @staticmethod
    def _elapsed_minutes(start_time: datetime, now: datetime) -> int:
        return max(0, int((now - start_time).total_seconds() / 60))

    @staticmethod
    def _recovery_minutes(bank: BankProfile, balance: int) -> int:
        assert bank.recovery_rate is not None
        return max(0, math.ceil((bank.max_balance_minutes - balance) / bank.recovery_rate - 1e-9))

    @staticmethod
    def _matching_day_banks(profile: RuleProfile, weekday: int) -> dict[str, BankProfile]:
        return {
            key: bank
            for key, bank in profile.banks.items()
            if bank.is_day_bank and weekday in (bank.days or ())
        }

    def _untapped_day_banks(
        self,
        now: datetime,
        profile: RuleProfile,
    ) -> dict[str, BankProfile]:
        local_date = now.date().isoformat()
        return {
            key: bank
            for key, bank in self._matching_day_banks(profile, now.weekday()).items()
            if not self._store.has_play_day_tap(self._child_id, bank.name, local_date)
        }

    def _limiting_balances(
        self,
        state: RuntimeState,
        profile: RuleProfile,
    ) -> dict[str, int]:
        return {
            key: balance
            for key, balance in state.bank_balances.items()
            if not profile.banks[key].is_day_bank
        }

    def _refresh_runtime(self, now: datetime, profile: RuleProfile) -> tuple[RuntimeState, AutomaticUpdate]:
        active = self._store.get_active_session(self._child_id)
        completed_minutes: int | None = None
        natural_end: datetime | None = None
        if active is not None:
            session_id, start_time, minutes_granted = active
            session_end = start_time + timedelta(minutes=minutes_granted)
            if now >= session_end:
                self._store.complete_session(session_id, session_end)
                self._store.clear_recovery_interruption(self._child_id)
                completed_minutes = minutes_granted
                natural_end = session_end
                active = None

        balances: dict[str, int] = {}
        recovered: list[str] = []
        for key, bank in profile.banks.items():
            balance, updated_at = self._store.get_bank_state(
                self._child_id,
                bank.name,
                bank.max_balance_minutes if bank.is_recovery else 0,
                now,
            )
            if bank.is_recovery and active is None and balance < bank.max_balance_minutes:
                recovery_started = max(updated_at, natural_end) if natural_end is not None else updated_at
                elapsed = max(0.0, (now - recovery_started).total_seconds() / 60)
                if elapsed >= self._recovery_minutes(bank, balance):
                    balance = bank.max_balance_minutes
                    self._store.set_bank_balance(self._child_id, balance, bank.name, now)
                    recovered.append(bank.name)
                elif recovery_started != updated_at:
                    self._store.set_bank_balance(self._child_id, balance, bank.name, recovery_started)
            balances[key] = balance

        if active is None:
            active_remaining = 0
        else:
            _session_id, start_time, minutes_granted = active
            active_remaining = max(0, minutes_granted - self._elapsed_minutes(start_time, now))
        return (
            RuntimeState(balances, active_remaining, active),
            AutomaticUpdate(recovered, completed_minutes),
        )

    def _bank_balance_line(self, bank: BankProfile, balance: int) -> str:
        if bank.is_day_bank:
            return self._i18n.msg(
                "rules.day_bank_balance",
                bank_name=bank.name,
                balance=balance,
                maximum=bank.max_balance_minutes,
                weekly_addition=bank.weekly_addition_minutes,
                days=", ".join(WEEKDAY_SUFFIXES[day].lower() for day in bank.days or ()),
            )
        if bank.is_recovery:
            return self._i18n.msg(
                "rules.recovery_bank_balance",
                bank_name=bank.name,
                balance=format_duration(balance),
                maximum=format_duration(bank.max_balance_minutes),
                recovery_rate=f"{bank.recovery_rate:g}",
            )
        assert bank.weekly_addition_minutes is not None
        return self._i18n.msg(
            "rules.bank_balance",
            bank_name=bank.name,
            balance=format_duration(balance),
            maximum=format_duration(bank.max_balance_minutes),
            weekly_addition=format_duration(bank.weekly_addition_minutes),
        )

    def _bank_pace_line(self, bank: BankProfile, balance: int, now: datetime, profile: RuleProfile) -> str:
        playable_days, non_blackout_remaining = self._remaining_playable_window(now, profile)
        avg_per_day = int(round(balance / playable_days)) if playable_days > 0 else 0
        week_non_blackout = self._this_week_non_blackout_minutes(profile)
        balance_share = balance / bank.max_balance_minutes
        non_blackout_share = non_blackout_remaining / week_non_blackout if week_non_blackout else 0.0
        pace_ratio = balance_share / non_blackout_share if non_blackout_share else 0.0
        return self._i18n.msg(
            "rules.status_bank_avg_pace",
            avg_per_day=format_duration(avg_per_day),
            balance_share=f"{balance_share * 100:.1f}%",
            non_blackout_share=f"{non_blackout_share * 100:.1f}%",
            pace_ratio=f"{pace_ratio:.2f}x",
        )

    def _bank_status_lines(
        self,
        balances: dict[str, int],
        now: datetime,
        profile: RuleProfile,
        active: ActiveSession | None,
    ) -> list[str]:
        lines: list[str] = []
        for key, bank in profile.banks.items():
            balance = balances[key]
            lines.append(self._bank_balance_line(bank, balance))
            if bank.is_day_bank:
                continue
            if not bank.is_recovery:
                lines.append(self._bank_pace_line(bank, balance, now, profile))
            elif balance < bank.max_balance_minutes:
                if active is not None:
                    lines.append(self._i18n.msg("rules.recovery_bank_waiting_for_break"))
                else:
                    _stored_balance, recovery_started = self._store.get_bank_state(self._child_id, bank.name)
                    elapsed = max(0, self._elapsed_minutes(recovery_started, now))
                    remaining = max(0, self._recovery_minutes(bank, balance) - elapsed)
                    lines.append(
                        self._i18n.msg(
                            "rules.recovery_bank_progress",
                            remaining=format_duration(remaining),
                        )
                    )
        return lines

    def _is_in_blackout_period(self, dt: datetime, profile: RuleProfile | None = None) -> tuple[bool, str]:
        weekday = dt.weekday()
        current_minutes = dt.hour * 60 + dt.minute
        for period_weekday, start, end, start_time, end_time in self._parsed_blackout_periods(
            profile or self._profile()
        ):
            if weekday == period_weekday and start <= current_minutes < end:
                return True, self._i18n.msg("rules.blackout_denied", start_time=start_time, end_time=end_time)
        return False, ""

    def _minutes_until_next_blackout(self, dt: datetime, profile: RuleProfile | None = None) -> int | None:
        parsed = self._parsed_blackout_periods(profile or self._profile())
        if not parsed:
            return None
        today = dt.replace(hour=0, minute=0, second=0, microsecond=0)
        best: int | None = None
        for day_offset in range(8):
            target_weekday = (dt.weekday() + day_offset) % 7
            day_start = today + timedelta(days=day_offset)
            for weekday, start, _end, _start_text, _end_text in parsed:
                if weekday != target_weekday:
                    continue
                start_dt = day_start + timedelta(minutes=start)
                if start_dt > dt:
                    candidate = int((start_dt - dt).total_seconds() // 60)
                    best = candidate if best is None else min(best, candidate)
        return best

    @staticmethod
    def _minutes_until_next_day(dt: datetime) -> int:
        tomorrow = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(0, int((tomorrow - dt).total_seconds() // 60))

    def _schedule_capacities(self, now: datetime, profile: RuleProfile) -> tuple[int | None, int | None]:
        blackout_capacity = self._minutes_until_next_blackout(now, profile)
        day_capacity = (
            self._minutes_until_next_day(now)
            if any(bank.is_day_bank for bank in profile.banks.values())
            else None
        )
        return blackout_capacity, day_capacity

    @staticmethod
    def _merged_minutes(ranges: list[tuple[int, int]]) -> int:
        if not ranges:
            return 0
        ranges = sorted(ranges)
        merged_start, merged_end = ranges[0]
        total = 0
        for start, end in ranges[1:]:
            if start <= merged_end:
                merged_end = max(merged_end, end)
            else:
                total += merged_end - merged_start
                merged_start, merged_end = start, end
        return total + merged_end - merged_start

    def _clipped_blackout_ranges(
        self,
        weekday: int,
        window_start: int,
        window_end: int,
        profile: RuleProfile,
    ) -> list[tuple[int, int]]:
        return [
            (max(window_start, start), min(window_end, end))
            for period_weekday, start, end, _start_text, _end_text in self._parsed_blackout_periods(profile)
            if period_weekday == weekday and max(window_start, start) < min(window_end, end)
        ]

    def _non_blackout_minutes_for_window(
        self,
        weekday: int,
        start: int,
        end: int,
        profile: RuleProfile,
    ) -> int:
        blocked = self._merged_minutes(self._clipped_blackout_ranges(weekday, start, end, profile))
        return max(0, end - start - blocked)

    def _remaining_playable_window(self, now: datetime, profile: RuleProfile) -> tuple[int, int]:
        days_until_monday = 7 - now.weekday()
        window_end = (now + timedelta(days=days_until_monday)).replace(hour=0, minute=0, second=0, microsecond=0)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        playable_days = total = 0
        day = today
        while day < window_end:
            start = now.hour * 60 + now.minute if day == today else 0
            playable = self._non_blackout_minutes_for_window(day.weekday(), start, 1440, profile)
            total += playable
            playable_days += playable > 0
            day += timedelta(days=1)
        return playable_days, total

    def _this_week_non_blackout_minutes(self, profile: RuleProfile) -> int:
        return sum(
            1440 - self._merged_minutes(self._clipped_blackout_ranges(weekday, 0, 1440, profile))
            for weekday in range(7)
        )

    def _max_target_minutes_from_now(
        self,
        state: RuntimeState,
        profile: RuleProfile,
        schedule_capacities: tuple[int | None, int | None],
        requested_minutes: int | None = None,
    ) -> int:
        schedule_capacity = min((c for c in schedule_capacities if c is not None), default=None)
        balances = self._limiting_balances(state, profile)
        if balances:
            target = state.active_remaining + max(0, min(balances.values()))
        elif requested_minutes is not None:
            target = requested_minutes
        else:
            # Day banks decide whether today may be used, not how many minutes it contains.
            target = schedule_capacity if schedule_capacity is not None else 1440
        if schedule_capacity is not None:
            target = min(target, schedule_capacity)
        return max(0, target)

    def _partial_grant_reason(
        self,
        requested: int,
        granted: int,
        state: RuntimeState,
        profile: RuleProfile,
        blackout_capacity: int | None,
        day_capacity: int | None,
    ) -> str:
        if granted >= requested:
            return ""
        lines = [
            self._i18n.msg(
                "rules.partial_grant_summary",
                requested=format_duration(requested),
                granted=format_duration(granted),
            )
        ]
        for key, balance in state.bank_balances.items():
            if profile.banks[key].is_day_bank:
                continue
            capacity = state.active_remaining + max(0, balance)
            if requested > capacity:
                lines.append(
                    self._i18n.msg(
                        "rules.partial_grant_reason_bank",
                        bank_name=profile.banks[key].name,
                        available=format_duration(capacity),
                    )
                )
        if blackout_capacity is not None and requested > blackout_capacity:
            lines.append(
                self._i18n.msg("rules.partial_grant_reason_blackout", available=format_duration(blackout_capacity))
            )
        if day_capacity is not None and requested > day_capacity:
            lines.append(
                self._i18n.msg("rules.partial_grant_reason_day_boundary", available=format_duration(day_capacity))
            )
        return "\n".join(lines)

    def evaluate_request(self, requested_minutes: int) -> PlaytimeDecision:
        now = self._get_local_now()
        profile = self._profile()
        in_blackout, reason = self._is_in_blackout_period(now, profile)
        if in_blackout:
            return PlaytimeDecision(False, reason)

        state, _update = self._refresh_runtime(now, profile)
        empty = [
            profile.banks[key].name
            for key, balance in self._limiting_balances(state, profile).items()
            if balance <= 0
        ]
        empty.extend(
            bank.name
            for key, bank in self._untapped_day_banks(now, profile).items()
            if state.bank_balances[key] <= 0
        )
        if empty and requested_minutes > state.active_remaining:
            return PlaytimeDecision(
                False,
                self._i18n.msg("rules.bank_empty_denied", bank_names=", ".join(empty)),
            )
        if state.active_remaining > 0 and requested_minutes <= state.active_remaining:
            return PlaytimeDecision(
                False,
                self._i18n.msg(
                    "rules.request_below_active_remaining",
                    requested=format_duration(requested_minutes),
                    remaining=format_duration(state.active_remaining),
                ),
            )

        schedule_capacities = self._schedule_capacities(now, profile)
        granted = min(
            max(0, requested_minutes),
            self._max_target_minutes_from_now(state, profile, schedule_capacities, requested_minutes),
        )
        if granted <= state.active_remaining:
            return PlaytimeDecision(
                False,
                self._i18n.msg(
                    "rules.cannot_grant_now",
                    banks=", ".join(
                        f"{profile.banks[key].name} {format_duration(balance)}"
                        for key, balance in state.bank_balances.items()
                    ),
                ),
            )
        return PlaytimeDecision(
            True,
            self._partial_grant_reason(
                requested_minutes,
                granted,
                state,
                profile,
                *schedule_capacities,
            ),
            granted,
        )

    def grant_playtime(self, minutes: int) -> tuple[int, str]:
        now = self._get_local_now()
        profile = self._profile()
        state, _update = self._refresh_runtime(now, profile)
        additional = max(0, minutes - state.active_remaining)

        interrupted: dict[str, datetime] = {}
        if state.active_session is None:
            for key, bank in profile.banks.items():
                if bank.is_recovery and state.bank_balances[key] < bank.max_balance_minutes:
                    _balance, started = self._store.get_bank_state(self._child_id, bank.name)
                    if started < now:
                        interrupted[key] = started
            if interrupted:
                self._store.set_recovery_interruption(self._child_id, interrupted, now)
            else:
                self._store.clear_recovery_interruption(self._child_id)

        new_balances = state.bank_balances.copy()
        for key, bank in profile.banks.items():
            if not bank.is_day_bank:
                new_balances[key] -= additional
                self._store.set_bank_balance(self._child_id, new_balances[key], bank.name, now)

        if state.active_session is None:
            session_id = self._store.add_session(self._child_id, now, minutes)
        else:
            session_id, _start, current_granted = state.active_session
            self._store.set_session_minutes_granted(session_id, current_granted + additional)
        for bank_name, bank in profile.banks.items():
            if not bank.is_day_bank:
                self._store.add_session_bank_debit(session_id, bank_name, additional)
        local_date = now.date().isoformat()
        for key, bank in self._untapped_day_banks(now, profile).items():
            if self._store.tap_play_day(self._child_id, bank.name, local_date, session_id):
                new_balances[key] -= 1

        lines = [self._i18n.msg("rules.grant_confirm", minutes=format_duration(minutes))]
        if state.active_session is not None:
            lines.append(self._i18n.msg("rules.added_now", minutes=format_duration(additional)))
        lines.extend(self._bank_status_lines(new_balances, now, profile, (session_id, now, minutes)))
        if interrupted:
            lines.append(self._i18n.msg("rules.recovery_interrupted_warning"))
        return session_id, "\n".join(lines)

    def complete_session(self) -> str:
        now = self._get_local_now()
        profile = self._profile()
        state, _update = self._refresh_runtime(now, profile)
        if state.active_session is None:
            return self._i18n.msg("rules.no_active_session")

        session_id, start_time, minutes_granted = state.active_session
        actual = min(self._elapsed_minutes(start_time, now), minutes_granted)
        unused = minutes_granted - actual
        if unused > 0:
            for bank_name, debited in self._store.get_session_bank_debits(session_id).items():
                refund = min(unused, debited)
                balance = self._store.get_bank_balance(self._child_id, bank_name)
                self._store.set_bank_balance(self._child_id, balance + refund, bank_name, now)
        self._store.complete_session(session_id, now)

        restored = False
        interruption = self._store.get_recovery_interruption(self._child_id)
        if interruption is not None:
            saved_starts, interrupted_at = interruption
            if 0 <= (now - interrupted_at).total_seconds() <= RECOVERY_RESTORE_GRACE_PERIOD_SECONDS:
                for bank_name, recovery_started in saved_starts.items():
                    balance = self._store.get_bank_balance(self._child_id, bank_name)
                    self._store.set_bank_balance(self._child_id, balance, bank_name, recovery_started)
                restored = True
            self._store.clear_recovery_interruption(self._child_id)

        message = self._i18n.msg("rules.session_completed", minutes=format_duration(actual))
        if unused > 0:
            message += self._i18n.msg("rules.session_completed_credited", minutes=format_duration(unused))
        if restored:
            message += "\n" + self._i18n.msg("rules.recovery_restored")
        balances, _ = self._refresh_runtime(now, profile)
        message += "\n" + "\n".join(
            self._bank_status_lines(balances.bank_balances, now, profile, None)
        )
        return message

    def has_active_session(self) -> bool:
        state, _update = self._refresh_runtime(self._get_local_now(), self._profile())
        return state.active_session is not None

    def check_automatic_updates(self) -> list[str]:
        state, update = self._refresh_runtime(self._get_local_now(), self._profile())
        messages: list[str] = []
        if update.naturally_completed_minutes is not None:
            messages.append(
                self._i18n.msg(
                    "rules.session_completed",
                    minutes=format_duration(update.naturally_completed_minutes),
                )
            )
        messages.extend(
            self._i18n.msg("rules.recovery_bank_completed", bank_name=bank_name)
            for bank_name in update.recovered_banks
        )
        return messages

    def get_status(self) -> str:
        now = self._get_local_now()
        profile = self._profile()
        state, _update = self._refresh_runtime(now, profile)
        lines = [
            self._i18n.msg("rules.status_title"),
            "",
            *self._bank_status_lines(state.bank_balances, now, profile, state.active_session),
            "",
        ]

        available = self._max_target_minutes_from_now(state, profile, self._schedule_capacities(now, profile))
        if any(
            state.bank_balances[key] <= 0
            for key in self._untapped_day_banks(now, profile)
        ):
            available = state.active_remaining
        weekday = now.weekday()
        current = now.hour * 60 + now.minute
        upcoming = [
            period
            for _start, period in sorted(
                (start, f"{start_text}-{end_text}")
                for period_weekday, start, end, start_text, end_text in self._parsed_blackout_periods(profile)
                if period_weekday == weekday and end > current
            )
        ]
        if self._is_in_blackout_period(now, profile)[0]:
            available = state.active_remaining if state.active_session is not None else 0
        if upcoming:
            lines.extend(
                [
                    self._i18n.msg("rules.status_today_current_and_upcoming_blackouts", periods=", ".join(upcoming)),
                    "",
                ]
            )
        if state.active_session is None:
            lines.append(self._i18n.msg("rules.status_can_play_now", minutes=format_duration(available)))
        else:
            lines.append(self._i18n.msg("rules.status_active_session", minutes=format_duration(state.active_remaining)))
            lines.append(
                self._i18n.msg(
                    "rules.status_can_request_additional",
                    minutes=format_duration(max(0, available - state.active_remaining)),
                )
            )
        return "\n".join(lines)

    def _resolve_bank(self, bank_name: str | None) -> BankProfile | None:
        profile = self._profile()
        return profile.default_bank if bank_name is None else profile.banks.get(bank_name.lower())

    def _unknown_bank_message(self, bank_name: str | None) -> str:
        return self._i18n.msg(
            "rules.bank_unknown",
            bank_name=bank_name or "",
            bank_names=", ".join(bank.name for bank in self._profile().banks.values()),
        )

    def add_to_bank(self, minutes: int, bank_name: str | None = None) -> str:
        bank = self._resolve_bank(bank_name)
        if bank is None:
            return self._unknown_bank_message(bank_name)
        now = self._get_local_now()
        self._refresh_runtime(now, self._profile())
        actual = self._store.add_to_bank(self._child_id, minutes, bank.max_balance_minutes, bank.name)
        balance = self._store.get_bank_balance(self._child_id, bank.name)
        if bank.is_day_bank:
            return self._i18n.msg(
                "rules.add_to_day_bank",
                bank_name=bank.name,
                actual_added=actual,
                bank=balance,
                max_bank=bank.max_balance_minutes,
            )
        return self._i18n.msg(
            "rules.add_to_bank",
            bank_name=bank.name,
            actual_added=format_duration(actual),
            bank=format_duration(balance),
            max_bank=format_duration(bank.max_balance_minutes),
        )

    def set_bank(self, minutes: int, bank_name: str | None = None) -> str:
        bank = self._resolve_bank(bank_name)
        if bank is None:
            return self._unknown_bank_message(bank_name)
        capped = max(0, min(minutes, bank.max_balance_minutes))
        old = self._store.get_bank_balance(self._child_id, bank.name)
        self._store.set_bank_balance(self._child_id, capped, bank.name, self._get_local_now())
        if bank.is_day_bank:
            message = self._i18n.msg(
                "rules.set_day_bank",
                bank_name=bank.name,
                old_balance=old,
                new_balance=capped,
                max_bank=bank.max_balance_minutes,
            )
            if capped != minutes:
                message += self._i18n.msg("rules.set_bank_capped")
            return message
        message = self._i18n.msg(
            "rules.set_bank",
            bank_name=bank.name,
            old_balance=format_duration(old),
            new_balance=format_duration(capped),
            max_bank=format_duration(bank.max_balance_minutes),
        )
        if capped != minutes:
            message += self._i18n.msg("rules.set_bank_capped")
        return message

    def modify_bank(self, delta_minutes: int, bank_name: str | None = None) -> str:
        bank = self._resolve_bank(bank_name)
        if bank is None:
            return self._unknown_bank_message(bank_name)
        self._refresh_runtime(self._get_local_now(), self._profile())
        current = self._store.get_bank_balance(self._child_id, bank.name)
        return self.set_bank(current + delta_minutes, bank.name)

    def rollover_week(self) -> str:
        profile = self._profile()
        lines = [self._i18n.msg("rules.rollover_week")]
        for bank in profile.banks.values():
            if bank.weekly_addition_minutes is None:
                continue
            old = self._store.get_bank_balance(self._child_id, bank.name)
            actual = self._store.add_to_bank(
                self._child_id,
                bank.weekly_addition_minutes,
                bank.max_balance_minutes,
                bank.name,
            )
            lines.append(
                self._i18n.msg(
                    "rules.rollover_day_bank" if bank.is_day_bank else "rules.rollover_bank",
                    bank_name=bank.name,
                    actual_added=actual if bank.is_day_bank else format_duration(actual),
                    new_bank=old + actual if bank.is_day_bank else format_duration(old + actual),
                    max_bank=(
                        bank.max_balance_minutes
                        if bank.is_day_bank
                        else format_duration(bank.max_balance_minutes)
                    ),
                )
            )
        return "\n".join(lines)
