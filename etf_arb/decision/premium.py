"""Independent premium/discount decision source. Engine does not reprice signals."""

from datetime import time, timedelta
from decimal import Decimal

from etf_arb.data.snapshot import component_symbol
from etf_arb.domain import Quote, Side, TradeDecision
from etf_arb.execution.plan import application_allowed
from etf_arb.pricing.errors import PricingDataError
from etf_arb.pricing.iopv import calculate_iopv_159338
from etf_arb.rules import is_continuous_auction_time


class PremiumDiscountSource:
    def __init__(self, prices, settings, available):
        self.prices, self.settings, self.available = prices, settings, available
        self.rows = []

    def on_time(self, context):
        now = context.now
        if not is_continuous_auction_time(now.time()):
            return ()
        pcf = context.pcfs["159338"]
        row = dict(ts=now, etf_close=None, iopv=None, premium=None, side="", reason="")
        self.rows.append(row)
        etf = self.prices.get("159338.SZ", now)
        quotes = {}
        for component in pcf.components:
            if component.is_cash_placeholder or component.substitution_flag in {"2", "必须", "必须现金替代"}:
                continue
            bar = self.prices.get(component_symbol(component), now)
            if bar:
                quotes[component.security_code] = Quote(component.security_code, bar.time, last=bar.close)
        try:
            valuation = calculate_iopv_159338(pcf, quotes, now)
        except PricingDataError as error:
            row["reason"] = f"unpriced: {error}"
            return ()
        if etf is None or now - etf.time > timedelta(minutes=1) or valuation.stale_quote_count:
            row["reason"] = "missing_or_stale_quote"
            return ()
        if valuation.iopv <= 0:
            row["reason"] = "nonpositive_iopv"
            return ()
        premium = etf.close / valuation.iopv - 1
        row.update(etf_close=etf.close, iopv=valuation.iopv, premium=premium)
        threshold = self.settings.premium_threshold
        # Compare numerator values to avoid rounding an exact 4% into a crossing.
        numerator = valuation.numerator
        market_value = etf.close * pcf.creation_unit
        side = (Side.CREATE if market_value > numerator * (1 + threshold) else
                Side.REDEEM if market_value < numerator * (1 - threshold) else None)
        if side is None:
            row["reason"] = "below_threshold"
            return ()
        row["side"] = side.value
        if not self.available(now.date()):
            row["reason"] = "active_trade_or_daily_limit"
            return ()
        # Leave two bars for building the basket and selling the received assets.
        if now.time() > time(14, 53):
            row["reason"] = "too_late_for_application"
            return ()
        rule = context.resources.rules.get(pcf.etf_code, now.date())
        if not application_allowed(pcf, rule, side, self.settings.quantity_units, now.time()):
            row["reason"] = "application_disabled_or_limit"
            return ()
        row["reason"] = "threshold_crossed"
        edge = abs(market_value - numerator) * self.settings.quantity_units
        return (TradeDecision(pcf.etf_code, now, True, side, self.settings.quantity_units,
                              edge, Decimal("0"), edge, ("premium_discount_threshold",)),)
