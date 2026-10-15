"""A-share continuous-auction sessions and daily price-limit rules.

The board, ST status and IPO trading-day ordinal must come from point-in-time
security master data. Never infer the IPO ordinal from calendar days.
Only ordinary A-share IPOs are covered; relistings and special exemptions
require their own effective-dated security rules.
"""

from __future__ import annotations

from datetime import date, datetime, time
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
_BOARD_RATE = {
    AShareBoard.MAIN: Decimal("0.10"),
    AShareBoard.CHINEXT: Decimal("0.20"),
    AShareBoard.STAR: Decimal("0.20"),
    AShareBoard.BSE: Decimal("0.30"),
}


def is_continuous_auction_time(at: time | datetime) -> bool:
    """Check local China-exchange wall-clock time; trading calendar is separate.

    Intervals are half-open: 11:30 and 14:57 are excluded. This is an
    execution/signal window, not a minute-bar timestamp convention.
    """
    clock = at.timetz().replace(tzinfo=None) if isinstance(at, datetime) else at
    if not isinstance(clock, time):
        raise TypeError("at must be a time or datetime")
    if clock.tzinfo is not None:
        raise ValueError("Use exchange-local naive time (Asia/Shanghai)")
    return any(start <= clock < end for start, end in CONTINUOUS_AUCTION_SESSIONS)


def a_share_price_limit_rate(
    board: AShareBoard | str,
    on: date,
    *,
    is_st: bool = False,
    ipo_trading_day: int | None = None,
) -> Decimal | None:
    """Return the symmetric daily price-limit fraction, or None if unlimited.

    ipo_trading_day is 1-based and counts actual *trading* days from IPO.
    Missing IPO-day data is not treated as a new listing.
    """
    if type(on) is not date:
        raise TypeError("on must be a date")
    try:
        resolved_board = AShareBoard(board)
    except ValueError as error:
        raise ValueError(f"Unsupported A-share board: {board!r}") from error
    if ipo_trading_day is not None:
        if type(ipo_trading_day) is not int or ipo_trading_day < 1:
            raise ValueError("ipo_trading_day must be a positive 1-based integer")
        exempt_days = 1 if resolved_board is AShareBoard.BSE else 5
        if ipo_trading_day <= exempt_days:
            return None
    if resolved_board is AShareBoard.MAIN and is_st and on < MAIN_ST_TEN_PERCENT_FROM:
        return Decimal("0.05")
    return _BOARD_RATE[resolved_board]


def make_a_share_security_rule(
    security_code: str,
    on: date,
    board: AShareBoard | str,
    *,
    is_st: bool = False,
    ipo_trading_day: int | None = None,
) -> SecurityTradingRule:
    """Build a daily snapshot compatible with the existing rule repository.

    No implicit assumptions are made for fees, short selling, lot size,
    tick size or other rules not specified by this module.
    """
    rate = a_share_price_limit_rate(
        board, on, is_st=is_st, ipo_trading_day=ipo_trading_day
    )
    return SecurityTradingRule(
        security_code=security_code,
        effective_from=on,
        effective_to=on,
        buy_enabled=True,
        sell_enabled=True,
        limit_up_rate=rate,
        limit_down_rate=rate,
        trading_start=CONTINUOUS_AUCTION_SESSIONS[0][0],
        trading_end=CONTINUOUS_AUCTION_SESSIONS[-1][1],
    )


def is_a_share_rule_trading_time(rule: SecurityTradingRule, at: time | datetime) -> bool:
    """Respect the security's broad window AND the actual two sessions."""
    clock = at.timetz().replace(tzinfo=None) if isinstance(at, datetime) else at
    return is_continuous_auction_time(at) and rule.is_trading_time(clock)


__all__ = [
    "AShareBoard", "CONTINUOUS_AUCTION_SESSIONS",
    "MAIN_ST_TEN_PERCENT_FROM", "a_share_price_limit_rate",
    "is_a_share_rule_trading_time", "is_continuous_auction_time",
    "make_a_share_security_rule",
]
