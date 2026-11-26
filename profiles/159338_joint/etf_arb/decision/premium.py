"""Independent premium/discount decision source. Engine does not reprice signals."""

from datetime import timedelta
import json

from etf_arb.data.snapshot import FULL_FLOW_ETFS, component_symbol
from etf_arb.costs import AssetType, TradeFeeModel
from etf_arb.decision.basket import check_creation_basket
from etf_arb.decision.limit_loss import LimitLossThreshold
from etf_arb.domain import Quote, Side, TradeDecision
from etf_arb.execution.plan import application_allowed, build_plan
from etf_arb.pricing.errors import PricingDataError
from etf_arb.pricing.iopv import calculate_iopv
from etf_arb.pricing.premium_discount import calculate_premium_discount
from etf_arb.rules import TradingTimeRule
from etf_arb.rules.price_limit_filter import price_limit_reason


class PremiumDiscountSource:
    def __init__(self, prices, settings, available, *, price_limit_filter=None, creation_guard=None,
                 reference_limits=None, dynamic_cash=None):
        if settings.effective_creation_threshold is None or settings.effective_redemption_threshold is None:
            raise ValueError("Set premium_threshold or both directional thresholds before running signals")
        self.prices, self.settings, self.available = prices, settings, available
        self.creation_guard = creation_guard
        self.reference_limits = reference_limits
        self.price_limit_filter = price_limit_filter
        self.dynamic_cash = dynamic_cash
        self.limit_loss = None
        if settings.limit_loss_threshold:
            if reference_limits is None:
                raise ValueError("Limit loss threshold requires initialized daily price limits")
            self.limit_loss = LimitLossThreshold(prices, reference_limits)
        self.rows = []
        self.fee_model = TradeFeeModel(settings.fees)
        self.trading_times = TradingTimeRule(settings.blocked_trade_minutes, settings.trading_sessions)

    def _static_valuation(self, pcf, now):
        quotes = {}
        for component in pcf.components:
            if component.is_cash_placeholder or component.substitution_flag in {
                "2", "必须", "必须现金替代", "深市必须", "沪市必须",
            }:
                continue
            bar = self.prices.get(component_symbol(component), now)
            if bar:
                quotes[component.security_code] = Quote(component.security_code, bar.time, last=bar.close)
        return calculate_iopv(pcf, quotes, now, session_validator=lambda at: self.trading_times.allows(at.time()))

    def on_time(self, context):
        now = context.now
        pcf, = context.pcfs.values()
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
        dynamic = self.dynamic_cash is not None
        symbol = FULL_FLOW_ETFS[pcf.etf_code]
        etf = self.prices.get(symbol, now, exact=True) if dynamic else self.prices.get(symbol, now)
        if dynamic and etf is not None:
            row["etf_close"] = etf.close
        try:
            if dynamic:
                row["pcf_cash_per_unit"] = pcf.estimated_cash_component
                valuation = self.dynamic_cash.value(pcf, now, self.prices)
                row.update(iopv=valuation.iopv, cash_adjustment_per_unit=valuation.cash_adjustment,
                           signal_cash_per_unit=valuation.estimated_cash,
                           security_value_per_unit=valuation.security_value,
                           fixed_cash_per_unit=valuation.mandatory_cash)
            else:
                valuation = self._static_valuation(pcf, now)
        except PricingDataError as error:
            row["reason"] = f"dynamic_unpriced: {error}" if dynamic else f"unpriced: {error}"
            return ()
        if etf is None or now - etf.time > timedelta(minutes=1) or valuation.stale_quote_count:
            row["reason"] = "dynamic_unpriced: missing_exact_etf_close" if dynamic else "missing_or_stale_quote"
            return ()
        if valuation.iopv <= 0:
            row["reason"] = "nonpositive_iopv"
            return ()
        premium = calculate_premium_discount(etf.close, valuation.iopv).premium_discount_rate
        row.update(etf_close=etf.close, iopv=valuation.iopv, premium=premium)
        # Compare numerator values to avoid rounding an exact threshold into a crossing.
        numerator = valuation.numerator
        market_value = etf.close * pcf.creation_unit
        create_limit = numerator * (1 + self.settings.effective_creation_threshold)
        redeem_limit = numerator * (1 - self.settings.effective_redemption_threshold)
        inclusive = dynamic and self.limit_loss is None
        side = None
        if market_value > create_limit or (inclusive and market_value == create_limit):
            side = Side.CREATE
        elif market_value < redeem_limit or (inclusive and market_value == redeem_limit):
            side = Side.REDEEM
        if side is None:
            row["reason"] = "below_threshold"
            return ()
        row["side"] = side.value
        if not self.settings.allows_side(side):
            row["reason"] = "direction_disabled"
            return ()
        units = self.settings.quantity_units
        edge = abs(market_value - numerator) * units
        if self.limit_loss is not None:
            basket_value = numerator * units
            base = (self.settings.effective_creation_threshold if side is Side.CREATE
                    else self.settings.effective_redemption_threshold)
            row.update(self.limit_loss.evaluate(pcf, now, side, units, basket_value, base))
            # Compare cash amounts so rounding the displayed ratio cannot change a boundary.
            if edge <= base * basket_value + row["estimated_limit_loss"]:
                row["reason"] = "below_limit_loss_threshold"
                return ()
        if self.price_limit_filter is not None:
            blocked = self.price_limit_filter.blocked(now)
            if blocked:
                row.update(reason=price_limit_reason(blocked), blocked_components=";".join(blocked),
                           component_reasons=json.dumps(blocked, ensure_ascii=False))
                return ()
        basket = None
        if side is Side.CREATE:
            basket = check_creation_basket(pcf, self.prices, context.resources.rules, now,
                                           self.settings.security_rule_policy, guard=self.creation_guard,
                                           reference_limits=self.reference_limits)
            row.update(basket.signal_fields())
            row["cash_substituted_components"] = ";".join(basket.cash_substitutions)
            if basket.blocked or basket.data_errors:
                row["blocked_components"] = ";".join(basket.blocked | basket.data_errors)
                row["component_reasons"] = json.dumps(basket.blocked | basket.data_errors, ensure_ascii=False)
                row["reason"] = basket.failure_reason
                return ()
        if not self.available(now):
            row["reason"] = "application_interval_or_pending_start"
            return ()
        if not any(clock > now.time() and self.trading_times.allows(clock)
                   for clock in context.resources.config.clock_times):
            row["reason"] = "no_later_entry_minute_today"
            return ()
        if self.settings.market_split_execution and not self.trading_times.has_next_minute(
            now, context.resources.config.clock_times
        ):
            row["reason"] = "no_exact_next_minute_for_market_split"
            return ()
        rule = context.resources.rules.get(pcf.etf_code, now.date())
        if not application_allowed(pcf, rule, side, self.settings.quantity_units, now.time()):
            row["reason"] = "application_disabled_or_limit"
            return ()
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
                              edge, cost, edge - cost, ("premium_discount_threshold",),
                              signal_cash_per_unit=row.get("signal_cash_per_unit")),)
