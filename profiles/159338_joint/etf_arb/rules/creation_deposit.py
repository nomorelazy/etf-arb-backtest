"""Price a 159338 domestic optional creation deposit from its contract reference."""

from datetime import datetime, time

from etf_arb.data.adjusted_references import ADJUSTED_REFERENCE_SOURCE
from etf_arb.data.snapshot import component_symbol


def creation_deposit_references(pcf, previous, adjusted):
    """Read each PCF's previous 15:00 once, even for suspended stocks with no limit bounds."""
    day = pcf.effective_date
    references = {}
    missing = set()
    for component in pcf.components:
        if component.is_cash_placeholder or component.quantity <= 0:
            continue
        symbol = component_symbol(component)
        if not symbol.endswith(".SZ"):
            continue
        reference = adjusted.limits.get((day, symbol))
        if reference is not None:
            references[symbol] = reference, ADJUSTED_REFERENCE_SOURCE
        elif (component.security_name or "").upper().lstrip().startswith(("XD", "XR", "DR")):
            references[symbol] = None, "missing_adjusted_deposit_reference"
        else:
            missing.add(symbol)
    prior = pcf.pre_trading_day
    if not missing or prior is None or prior >= day:
        return references
    at = datetime.combine(prior, time(15))
    previous.load(prior, missing, at=at.time())
    for symbol in missing:
        bar = previous.get(symbol, at, exact=True)
        if bar is None:
            continue
        basis = "pcf_previous_1500_close"
        if ((symbol, at) in getattr(previous, "rows", {})
                and previous.prices.get(symbol, at, exact=True) is None):
            basis = "sourced_pcf_previous_close"
        references[symbol] = bar.close, basis
    return references
