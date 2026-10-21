"""Independent premium/discount decision source. Engine does not reprice signals."""

from datetime import timedelta
import json

from etf_arb.data.snapshot import component_symbol
from etf_arb.costs import AssetType, TradeFeeModel
from etf_arb.decision.basket import check_creation_basket
from etf_arb.domain import Quote, Side, TradeDecision
from etf_arb.execution.plan import application_allowed, build_plan
from etf_arb.pricing.errors import PricingDataError
from etf_arb.pricing.iopv import calculate_iopv_159338
from etf_arb.pricing.premium_discount import calculate_premium_discount
from etf_arb.rules import TradingTimeRule


class PremiumDiscountSource:
    def __init__(self, prices, settings, available):
        if settings.premium_threshold is None:
            raise ValueError("Set simulation.premium_threshold or --premium-threshold explicitly before running signals")
        self.prices, self.settings, self.available = prices, settings, available
        self.rows = []
        self.fee_model = TradeFeeModel(settings.fees)
        self.trading_times = TradingTimeRule(settings.blocked_trade_minutes, settings.trading_sessions)

    def on_time(self, context):
        now = context.now
        pcf = context.pcfs["159338"]
        row = dict(ts=now, etf_close=None, iopv=None, premium=None, side="", reason="",
                   blocked_components="", component_reasons="", cash_substituted_components="",
                   estimated_fee=None, estimated_net_edge=None)
        self.rows.append(row)
        if self.trading_times.is_blocked(now.time()):
            row["reason"] = "blocked_trade_minute"
            return ()
        if not self.trading_times.allows(now.time()):
            row["reason"] = "outside_trading_sessions"
            return ()
        etf = self.prices.get("159338.SZ", now)
        quotes = {}
        for component in pcf.components:
            if component.is_cash_placeholder or component.substitution_flag in {"2", "必须", "必须现金替代"}:
                continue
            bar = self.prices.get(component_symbol(component), now)
            if bar:
                quotes[component.security_code] = Quote(component.security_code, bar.time, last=bar.close)
        try:
            valuation = calculate_iopv_159338(
                pcf, quotes, now, session_validator=lambda at: self.trading_times.allows(at.time()),
            )
        except PricingDataError as error:
            row["reason"] = f"unpriced: {error}"
            return ()
        if etf is None or now - etf.time > timedelta(minutes=1) or valuation.stale_quote_count:
            row["reason"] = "missing_or_stale_quote"
            return ()
        if valuation.iopv <= 0:
            row["reason"] = "nonpositive_iopv"
            return ()
        premium = calculate_premium_discount(etf.close, valuation.iopv).premium_discount_rate
        row.update(etf_close=etf.close, iopv=valuation.iopv, premium=premium)
        threshold = self.settings.premium_threshold
        # Compare numerator values to avoid rounding an exact threshold into a crossing.
        numerator = valuation.numerator
        market_value = etf.close * pcf.creation_unit
        side = (Side.CREATE if market_value > numerator * (1 + threshold) else
                Side.REDEEM if market_value < numerator * (1 - threshold) else None)
        if side is None:
            row["reason"] = "below_threshold"
            return ()
        row["side"] = side.value
        basket = None
        if side is Side.CREATE:
            basket = check_creation_basket(pcf, self.prices, context.resources.rules, now,
                                           self.settings.security_rule_policy)
            row["cash_substituted_components"] = ";".join(basket.cash_substitutions)
            if basket.blocked or basket.data_errors:
                row["blocked_components"] = ";".join(basket.blocked | basket.data_errors)
                row["component_reasons"] = json.dumps(basket.blocked | basket.data_errors, ensure_ascii=False)
                row["reason"] = ("unbuyable_and_no_cash_substitution" if basket.blocked
                                 else "missing_cash_substitution_data")
                return ()
        if not self.available(now):
            row["reason"] = "application_interval_or_pending_start"
            return ()
        if not any(clock > now.time() and self.trading_times.allows(clock)
                   for clock in context.resources.config.clock_times):
            row["reason"] = "no_later_entry_minute_today"
            return ()
        rule = context.resources.rules.get(pcf.etf_code, now.date())
        if not application_allowed(pcf, rule, side, self.settings.quantity_units, now.time()):
            row["reason"] = "application_disabled_or_limit"
            return ()
        edge = abs(market_value - numerator) * self.settings.quantity_units
        try:
            plan = build_plan(pcf, side, self.settings.quantity_units,
                              creation_cash_substitutions=basket.cash_substitutions if basket else None)
            etf_side = "SELL" if side is Side.CREATE else "BUY"
            stock_side = "BUY" if side is Side.CREATE else "SELL"
            cost = self.fee_model.calculate(asset_type=AssetType.ETF, side=etf_side,
                                           turnover=etf.close * plan.etf_shares).total
            for symbol, quantity in (plan.physical | plan.manager).items():
                bar = self.prices.get(symbol, now)
                if bar is None:
                    raise ValueError(f"Missing fee estimate quote: {symbol}")
                cost += self.fee_model.calculate(asset_type=AssetType.STOCK, side=stock_side,
                                                turnover=bar.close * quantity).total
        except ValueError as error:
            row["reason"] = f"unexecutable_plan: {error}"
            return ()
        row.update(reason="threshold_crossed", estimated_fee=cost, estimated_net_edge=edge - cost)
        return (TradeDecision(pcf.etf_code, now, True, side, self.settings.quantity_units,
                              edge, cost, edge - cost, ("premium_discount_threshold",)),)
