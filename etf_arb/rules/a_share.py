"""A-share continuous-auction sessions and daily price-limit rules.

The board, ST status and IPO trading-day ordinal must come from point-in-time
security master data. Never infer the IPO ordinal from calendar days.
Only ordinary A-share IPOs are covered; relistings and special exemptions
require their own effective-dated security rules.
"""

from __future__ import annotations

from datetime import date, datetime, time
from collections.abc import Iterable
from decimal import Decimal
from enum import Enum

from etf_arb.rules.models import SecurityTradingRule


class AShareBoard(str, Enum):
    MAIN = "main"           # 沪深主板
    CHINEXT = "chinext"     # 创业板
    STAR = "star"           # 科创板
    BSE = "bse"             # 北交所


CONTINUOUS_AUCTION_SESSIONS: tuple[tuple[time, time], ...] = (
    (time(9, 30), time(11, 30)),
    (time(13, 0), time(14, 57)),
)
MAIN_ST_TEN_PERCENT_FROM = date(2026, 7, 6)
DELISTING_PERIOD_TRADING_DAYS = 15
DELISTING_MAX_SUSPENDED_TRADING_DAYS = 5

# 沪深主板 ST/*ST：历史回测必须按交易日选择生效版本。
# 2026-07-06（含）之前的旧版本止于 2026-07-05；当天起执行 ±10%。
# 适用于普通交易日；IPO 无涨跌幅限制的交易日优先豁免。
MAIN_ST_PRICE_LIMIT_HISTORY: tuple[tuple[date, Decimal], ...] = (
    (date.min, Decimal("0.05")),
    (MAIN_ST_TEN_PERCENT_FROM, Decimal("0.10")),
)

_BOARD_RATE = {
    AShareBoard.MAIN: Decimal("0.10"),
    AShareBoard.CHINEXT: Decimal("0.20"),
    AShareBoard.STAR: Decimal("0.20"),
    AShareBoard.BSE: Decimal("0.30"),
}


def _beijing_naive_clock(at: time | datetime) -> time:
    """Accept only timezone-naive Beijing wall-clock values from market data.

    Aware values must fail before datetime.time() discards tzinfo. Do not
    implicitly convert to Beijing time or silently strip a timezone.
    """
    if not isinstance(at, (datetime, time)):
        raise TypeError("at must be a time or datetime")
    if at.tzinfo is not None:
        raise ValueError(
            "at must be timezone-naive Beijing local time; "
            "timezone-aware time/datetime is not supported"
        )
    return at.time() if isinstance(at, datetime) else at


def is_continuous_auction_time(at: time | datetime) -> bool:
    """Check naive Beijing-local clock time; trading calendar is separate.

    Intervals are half-open: 11:30 and 14:57 are excluded. This is an
    execution/signal window, not a minute-bar timestamp convention.
    """
    clock = _beijing_naive_clock(at)
    return any(start <= clock < end for start, end in CONTINUOUS_AUCTION_SESSIONS)


def delisting_trading_day_index(
    on: date,
    exchange_trading_days: Iterable[date],
    *,
    suspended_days: Iterable[date] = (),
) -> int | None:
    """Count delisting trading days from the first eligible day, excluding suspensions.

    exchange_trading_days MUST start at the first delisting-period trading
    day and contain every exchange trading day up to the queried date, in
    ascending order. Suspended whole days remain in this calendar but are
    excluded from the ordinal. Do not pass calendar days or infer holidays.

    Returns None for a suspended day or an exchange non-trading day. Returns
    16 or greater after the 15th eligible day, allowing fail-closed handling.
    """
    if type(on) is not date:
        raise TypeError("on must be a date")
    days = tuple(exchange_trading_days)
    if not days or any(type(day) is not date for day in days):
        raise ValueError("exchange_trading_days must be nonempty date values")
    if tuple(sorted(set(days))) != days:
        raise ValueError("exchange_trading_days must be strictly increasing and unique")
    suspended = tuple(suspended_days)
    if any(type(day) is not date for day in suspended):
        raise ValueError("suspended_days must contain dates")
    suspended_set = set(suspended)
    if len(suspended_set) != len(suspended):
        raise ValueError("suspended_days must not contain duplicates")
    if not suspended_set.issubset(set(days)):
        raise ValueError("suspended_days must be drawn from exchange_trading_days")
    if len(suspended_set) > DELISTING_MAX_SUSPENDED_TRADING_DAYS:
        raise ValueError("Delisting-period whole-day suspensions cannot exceed 5 trading days")
    if on not in days or on in suspended_set:
        return None
    return sum(day <= on and day not in suspended_set for day in days)


def a_share_price_limit_rate(
    board: AShareBoard | str,
    on: date,
    *,
    is_st: bool = False,
    ipo_trading_day: int | None = None,
    is_delisting_period: bool = False,
    delisting_trading_day: int | None = None,
    is_suspended: bool = False,
) -> Decimal | None:
    """Return the symmetric price-limit rate (None = no applicable limit).

    On delisting day 1 there is no limit; on days 2..15 the board's normal
    rate applies and overrides ST, even for historical dates before 2026-07-06.
    Delisting trading days EXCLUDE full-day suspensions. No price-limit rate
    alone implies that a suspended or delisted security is tradable.
    """
    if type(on) is not date:
        raise TypeError("on must be a date")
    try:
        resolved_board = AShareBoard(board)
    except ValueError as error:
        raise ValueError(f"Unsupported A-share board: {board!r}") from error
    for name, flag in (
        ("is_st", is_st),
        ("is_delisting_period", is_delisting_period),
        ("is_suspended", is_suspended),
    ):
        if type(flag) is not bool:
            raise ValueError(f"{name} must be a bool")
    if not is_delisting_period and delisting_trading_day is not None:
        raise ValueError("delisting_trading_day requires is_delisting_period=True")
    if is_delisting_period:
        if ipo_trading_day is not None:
            raise ValueError("IPO and delisting periods are mutually exclusive")
        if delisting_trading_day is not None and (
            type(delisting_trading_day) is not int or delisting_trading_day < 1
        ):
            raise ValueError("delisting_trading_day must be a positive integer")
        if is_suspended:
            if delisting_trading_day is not None:
                raise ValueError("Suspended days must not advance delisting_trading_day")
            return None
        if delisting_trading_day is None:
            raise ValueError("Active delisting periods require delisting_trading_day")
        if delisting_trading_day > DELISTING_PERIOD_TRADING_DAYS:
            # Stale delisting flag after the 15-day window: block execution.
            return None
        if delisting_trading_day == 1:
            return None
        return _BOARD_RATE[resolved_board]
    if ipo_trading_day is not None:
        if type(ipo_trading_day) is not int or ipo_trading_day < 1:
            raise ValueError("ipo_trading_day must be a positive 1-based integer")
        exempt_days = 1 if resolved_board is AShareBoard.BSE else 5
        if ipo_trading_day <= exempt_days:
            return None
    if resolved_board is AShareBoard.MAIN and is_st:
        # Resolve the last effective version <= the requested backtest date.
        # Never apply the current rule to a historical date.
        return next(
            rate
            for effective_from, rate in reversed(MAIN_ST_PRICE_LIMIT_HISTORY)
            if on >= effective_from
        )
    return _BOARD_RATE[resolved_board]


def make_a_share_security_rule(
    security_code: str,
    on: date,
    board: AShareBoard | str,
    *,
    is_st: bool = False,
    ipo_trading_day: int | None = None,
    is_delisting_period: bool = False,
    delisting_trading_day: int | None = None,
    is_suspended: bool = False,
) -> SecurityTradingRule:
    """Build a daily snapshot compatible with the existing rule repository.

    No implicit assumptions are made for fees, short selling, lot size,
    tick size or other rules not specified by this module.
    """
    rate = a_share_price_limit_rate(
        board, on, is_st=is_st, ipo_trading_day=ipo_trading_day,
        is_delisting_period=is_delisting_period,
        delisting_trading_day=delisting_trading_day,
        is_suspended=is_suspended,
    )
    tradable = not is_suspended and not (
        is_delisting_period
        and (delisting_trading_day is None or delisting_trading_day > DELISTING_PERIOD_TRADING_DAYS)
    )
    return SecurityTradingRule(
        security_code=security_code,
        effective_from=on,
        effective_to=on,
        buy_enabled=tradable,
        sell_enabled=tradable,
        is_st=is_st,
        is_delisting_period=is_delisting_period,
        is_suspended=is_suspended,
        delisting_trading_day=delisting_trading_day,
        limit_up_rate=rate,
        limit_down_rate=rate,
        trading_start=CONTINUOUS_AUCTION_SESSIONS[0][0],
        trading_end=CONTINUOUS_AUCTION_SESSIONS[-1][1],
    )


def is_a_share_rule_trading_time(rule: SecurityTradingRule, at: time | datetime) -> bool:
    """Respect the two sessions; reject timezone-aware timestamps."""
    clock = _beijing_naive_clock(at)
    return (
        (rule.can_buy() or rule.can_sell())
        and is_continuous_auction_time(clock)
        and rule.is_trading_time(clock)
    )


__all__ = [
    "AShareBoard", "CONTINUOUS_AUCTION_SESSIONS",
    "MAIN_ST_TEN_PERCENT_FROM", "MAIN_ST_PRICE_LIMIT_HISTORY",
    "DELISTING_PERIOD_TRADING_DAYS", "DELISTING_MAX_SUSPENDED_TRADING_DAYS",
    "delisting_trading_day_index",
    "a_share_price_limit_rate",
    "is_a_share_rule_trading_time", "is_continuous_auction_time",
    "make_a_share_security_rule",
]
