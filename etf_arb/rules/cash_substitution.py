"""Ordinary-case pricing and notice dates; a deadline is not a payment record."""

from dataclasses import dataclass
from datetime import datetime, time


@dataclass(frozen=True)
class CashSubstitutionTiming:
    pricing_trading_days: int = 2
    attempt_time: time | str = time(14, 30)

    def __post_init__(self):
        if type(self.pricing_trading_days) is not int or self.pricing_trading_days < 0:
            raise ValueError("pricing_trading_days must be a nonnegative integer")
        clock = self.attempt_time
        if isinstance(clock, str):
            if len(clock) != 5:
                raise ValueError("cash_substitution_attempt_time must use HH:MM")
            clock = time.fromisoformat(clock)
            if clock.strftime('%H:%M') != self.attempt_time:
                raise ValueError("cash_substitution_attempt_time must use HH:MM")
        if not isinstance(clock, time) or clock.tzinfo is not None or clock.second or clock.microsecond or clock >= time(15):
            raise ValueError("cash_substitution_attempt_time must be a naive minute before 15:00")
        object.__setattr__(self, 'attempt_time', clock)

    def attempt_at(self, pricing_day):
        return None if pricing_day is None else datetime.combine(pricing_day, self.attempt_time)

    def dates(self, applied_on, trading_days, business_days=None):
        index = trading_days.index(applied_on) + self.pricing_trading_days
        deadline = trading_days[index] if index < len(trading_days) else None
        # Workdays must be supplied explicitly; exchange trading days are not
        # silently treated as the fund/settlement agent's business calendar.
        later = [] if deadline is None or business_days is None else [d for d in business_days if d > deadline]
        notice = later[0] if later else None
        latest = later[3] if len(later) >= 4 else None
        return deadline, notice, latest
