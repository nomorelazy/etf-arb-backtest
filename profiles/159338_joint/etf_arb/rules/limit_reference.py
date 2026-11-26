"""Reference prices for limit estimates under the configured research policy.

The exact 09:30 close is an explicitly requested opening-price proxy, not an
exchange opening price. It only fills a missing prior-close reference. Callers
must not expose bounds based on it before 09:30 on the effective day.
"""

from datetime import datetime, time

from etf_arb.data.adjusted_references import ADJUSTED_REFERENCE_SOURCE
from etf_arb.data.minute_close import MinuteCloseBar


OPENING_PROXY_TIME = time(9, 30)
OPENING_PROXY_SOURCE = "estimated_open_0930_proxy"


def load_limit_references(pcf, symbols, previous, opening, adjusted=None):
    """Select effective-day adjusted references, then exact prior or 09:30 closes.

Official limit prices are resolved by the caller before passing its remaining
symbols. Missing opening bars stay unknown; no nearest-bar or cross-day lookup
is permitted. Board, ST, corporate-action and tick rules remain caller-owned.
    """
    symbols = tuple(sorted(symbols))
    if not symbols:
        return {}
    day = pcf.effective_date
    result = {}
    if adjusted is not None:
        for symbol in symbols:
            price = adjusted.limits.get((day, symbol))
            if price is not None:
                bar = MinuteCloseBar(datetime.combine(day, time.min), symbol, price)
                result[symbol] = bar, ADJUSTED_REFERENCE_SOURCE
        symbols = [symbol for symbol in symbols if symbol not in result]
        if not symbols:
            return result
    prior = pcf.pre_trading_day
    prior_at = None
    if prior is not None and prior < day:
        prior_at = datetime.combine(prior, time(15))
        previous.load(prior, symbols, at=prior_at.time())
    missing = []
    for symbol in symbols:
        bar = previous.get(symbol, prior_at, exact=True) if prior_at else None
        result[symbol] = (bar, "estimated" if bar else "missing_previous_close")
        if bar is None:
            missing.append(symbol)
    if missing:
        opening_at = datetime.combine(day, OPENING_PROXY_TIME)
        opening.load(day, missing, at=OPENING_PROXY_TIME)
        for symbol in missing:
            bar = opening.get(symbol, opening_at, exact=True)
            if bar is not None:
                result[symbol] = (bar, OPENING_PROXY_SOURCE)
    return result


def opening_proxy_is_future(source, now):
    """Do not allow a day-open preload to leak the 09:30 observation earlier."""
    return source == OPENING_PROXY_SOURCE and now.time() < OPENING_PROXY_TIME
