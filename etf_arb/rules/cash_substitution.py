"""Ordinary-case pricing and notice dates; a deadline is not a payment record."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime, time


# Minute-close research clock for stock liquidation and manager proxies.
SH_PROXY_MINUTES = tuple(
    time(minute // 60, minute % 60)
    for start, end in ((9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60))
    for minute in range(start, end + 1)
)


def next_stock_execution_minute(now, trading_days, deadline=None):
    """Next session minute on the supplied calendar, strictly after now."""
    index = bisect_left(trading_days, now.date())
    minute = 0
    if index < len(trading_days) and trading_days[index] == now.date():
        minute = bisect_right(SH_PROXY_MINUTES, now.time())
        if minute == len(SH_PROXY_MINUTES):
            index += 1
            minute = 0
    if index < len(trading_days) and (deadline is None or trading_days[index] <= deadline):
        return datetime.combine(trading_days[index], SH_PROXY_MINUTES[minute])
    return None


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

    def allows_sh_attempt(self, at):
        return at.second == 0 and at.microsecond == 0 and at.time() in SH_PROXY_MINUTES

    def next_sh_attempt(self, now, trading_days, deadline):
        return next_stock_execution_minute(now, trading_days, deadline)

    def dates(self, applied_on, trading_days, business_days=None):
        index = trading_days.index(applied_on) + self.pricing_trading_days
        deadline = trading_days[index] if index < len(trading_days) else None
        # Workdays must be supplied explicitly; exchange trading days are not
        # silently treated as the fund/settlement agent's business calendar.
        later = [] if deadline is None or business_days is None else [d for d in business_days if d > deadline]
        notice = later[0] if later else None
        latest = later[3] if len(later) >= 4 else None
        return deadline, notice, latest
