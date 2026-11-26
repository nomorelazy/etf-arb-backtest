"""Creation-only buy feasibility and optional domestic cash-substitution cap."""

from datetime import datetime, time
from decimal import Decimal

from etf_arb.data.adjusted_references import ADJUSTED_REFERENCE_SOURCE, AdjustedReferences
from etf_arb.data.snapshot import FULL_FLOW_ETFS, component_symbol
from etf_arb.rules.price_limit_filter import PriceLimitFilter


def load_reference_prices(path):
    return AdjustedReferences(explicit_path=path).prices


class CreationBasketGuard:
    def __init__(self, prices, minute_root, *, bounds_path=None, reference_prices_path=None, previous_closes=None,
                 adjusted_references=None):
        adjusted = adjusted_references
        if adjusted is None:
            adjusted = AdjustedReferences(minute_root, reference_prices_path)
        self.limits = PriceLimitFilter(prices, minute_root, bounds_path, previous_closes=previous_closes,
                                       adjusted_references=adjusted)
        self.previous = self.limits.previous
        self.provided_references = adjusted.prices
        self.pcf = None

    def on_day_open(self, pcf, rules, *, held_components=(), check_basket=True):
        self.pcf = pcf
        self.components = {component_symbol(c): c for c in pcf.components
                           if not c.is_cash_placeholder and c.quantity > 0}
        self.limits.on_day_open(pcf, rules, held_components=held_components, check_basket=check_basket)
        self.reference_loaded = False

    def buy_block(self, symbol, now):
        if self.pcf is None or self.pcf.effective_date != now.date():
            return "unknown:creation_guard_not_initialized"
        status = self.limits.status(symbol, now)
        # A lower limit does not prevent buying. The existing security rules
        # still independently cover suspensions and explicit BUY prohibitions.
        return status if status and not status.startswith("limit_down:") else ""

    def _reference(self, symbol):
        pcf = self.pcf
        key = pcf.effective_date, symbol
        provided = self.provided_references.get(key)
        if provided is not None:
            basis = ADJUSTED_REFERENCE_SOURCE if provided == self.limits.adjusted.limits.get(key) else "provided_reference"
            return provided, basis
        component = self.components.get(symbol)
        name = (component.security_name or "").upper().lstrip() if component else ""
        if name.startswith(("N", "C", "XD", "XR", "DR")):
            return None, "needs_adjusted_reference_price"
        if symbol == FULL_FLOW_ETFS[pcf.etf_code] and pcf.dividend_per_cu:
            return None, "needs_adjusted_etf_reference_price"
        previous_day = pcf.pre_trading_day
        if previous_day is None or previous_day >= pcf.effective_date:
            return None, "missing_previous_trading_day"
        if not self.reference_loaded:
            # Exactly the labelled prior close, once per day. Never scan back
            # through history or use a current/future price to fill a gap.
            symbols = set(self.components) | {FULL_FLOW_ETFS[pcf.etf_code]}
            self.previous.load(previous_day, symbols, at=time(15))
            self.reference_loaded = True
        bar = self.previous.get(symbol, datetime.combine(previous_day, time(15)), exact=True)
        return (bar.close, "previous_close_proxy") if bar else (None, "missing_previous_close")

    def check_cash_ratio(self, pcf, result):
        """Cap the chosen optional domestic stock value, excluding the premium.

        Mandatory fixed cash, cross-market proxy cash and estimated cash do
        not consume this optional physical-basket substitution allowance.
        Both values are per creation unit, so order size cancels in the ratio.
        """
        result.max_cash_substitution_ratio = pcf.max_cash_ratio
        if not result.cash_substitutions:
            result.cash_substitution_ratio = Decimal(0)
            result.cash_substitution_reference_value = Decimal(0)
            return
        if self.pcf is None or self.pcf != pcf:
            result.data_errors[pcf.etf_code] = "creation_guard_pcf_mismatch"
            return
        limit = pcf.max_cash_ratio
        if limit is None or not limit.is_finite() or not Decimal(0) <= limit <= Decimal(1):
            result.data_errors[pcf.etf_code] = "missing_or_invalid_max_cash_ratio"
            return
        if result.blocked or result.data_errors:
            return
        etf_symbol = FULL_FLOW_ETFS[pcf.etf_code]
        etf_reference, basis = self._reference(etf_symbol)
        if etf_reference is None:
            result.data_errors[etf_symbol] = f"cash_ratio_reference:{basis}"
            return
        value = Decimal(0)
        bases = {basis}
        for symbol in result.cash_substitutions:
            reference, basis = self._reference(symbol)
            if reference is None:
                result.data_errors[symbol] = f"cash_ratio_reference:{basis}"
                continue
            value += self.components[symbol].quantity * reference
            bases.add(basis)
        if result.data_errors:
            return
        denominator = Decimal(pcf.creation_unit) * etf_reference
        result.cash_substitution_reference_value = value
        result.cash_substitution_ratio = value / denominator
        result.cash_substitution_reference_basis = ";".join(sorted(bases))
        # Compare amounts directly: rounding the displayed ratio must not
        # admit an over-limit basket. Equality is allowed.
        if value > limit * denominator:
            result.cash_ratio_exceeded = True
            result.blocked[etf_symbol] = (
                f"cash_substitution_ratio_exceeded:{value}/{denominator}>{limit}"
            )
