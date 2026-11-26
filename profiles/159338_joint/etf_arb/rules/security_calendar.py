"""159338 pricing dates for cash substitutes, using exchange sessions and dated halts."""

from bisect import bisect_left, bisect_right
from datetime import datetime, time

from etf_arb.data.pcf import FilePCFRepository
from etf_arb.data.snapshot import DailyCloseCache
from etf_arb.report.prior_closes import PriorCloses, report_prior_path
from etf_arb.rules.errors import RuleNotFoundError


def _normal_session(symbol, day, rules):
    codes = (symbol, symbol.split(".")[0])
    for code in codes:
        try:
            return not rules.get_security_rule(code, day).is_suspended
        except RuleNotFoundError:
            continue
    history = getattr(rules, "security_rules", None)
    if history and any(history(code) for code in codes):
        return None  # A gap in an explicit security history is not evidence of trading.
    return True  # The dated suspension feed lists exceptions to the exchange calendar.


def substitution_deadline(symbol, applied_on, trading_days, rules):
    start = bisect_left(trading_days, applied_on)
    if start == len(trading_days) or trading_days[start] != applied_on:
        return None, False
    count = 0
    for elapsed, day in enumerate(trading_days[start + 1:start + 20], start=2):
        normal = _normal_session(symbol, day, rules)
        if normal is None:
            return None, False
        count += int(normal)
        if count == 2:
            return day, False
        if elapsed == 20:
            return day, True
    return None, False


def exception_close(symbol, as_of, trading_days, rules, config):
    """Look up the latest actual close at the cap, skipping only verified halts."""
    previous_days = list(trading_days[:bisect_right(trading_days, as_of)])
    if not previous_days or previous_days[-1] != as_of:
        return None
    prices = PriorCloses(DailyCloseCache(config.minute_root), report_prior_path(config))
    while previous_days:
        day = previous_days.pop()
        normal = _normal_session(symbol, day, rules)
        if normal is None:
            return None
        if normal:
            prices.load(day, [symbol], at=time(15))
            return prices.get(symbol, datetime.combine(day, time(15)), exact=True)
        if not previous_days:
            # A run may begin during a halt. PCF dates link back to the prior
            # trading day, even across a weekend or the beginning of the run.
            try:
                prior = FilePCFRepository(config.pcf_root).get("159338", day).pre_trading_day
            except FileNotFoundError:
                return None
            if prior is None or prior >= day:
                return None
            previous_days.append(prior)
    return None


def notice_dates(deadline, business_days, *, cap_exchange_days=None):
    if deadline is None:
        return None, None
    if cap_exchange_days is not None:
        i = bisect_right(cap_exchange_days, deadline)
        notice = cap_exchange_days[i] if i < len(cap_exchange_days) else None
    elif business_days:
        i = bisect_right(business_days, deadline)
        notice = business_days[i] if i < len(business_days) else None
    else:
        notice = None
    if notice is None or not business_days:
        return notice, None
    i = bisect_right(business_days, notice)
    latest = business_days[i + 2] if i + 2 < len(business_days) else None
    return notice, latest
