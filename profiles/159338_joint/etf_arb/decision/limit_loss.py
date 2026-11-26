"""Estimate the directional stock price loss from information available at the signal."""

import json
from decimal import Decimal

from etf_arb.data.snapshot import component_symbol
from etf_arb.domain import Side
from etf_arb.pricing.iopv import SubstitutionKind, _normalize_substitution_flag
from etf_arb.rules.limit_loss_calibration import estimate_limit_loss


AUDIT_FIELDS = (
    "base_threshold", "signal_basket_value", "estimated_limit_loss", "limit_loss_threshold_addon",
    "effective_threshold", "limit_loss_hit_count", "limit_loss_unknown_count",
    "limit_loss_components", "limit_loss_unknown_components",
)


class LimitLossThreshold:
    """A cash hurdle in yuan. Direction and the base signal still come from the strategy."""

    def __init__(self, prices, limits):
        self.prices = prices
        self.limits = limits
        self.pcf = None
        self.components = {}

    def evaluate(self, pcf, now, side, units, basket_value, base_threshold):
        if self.pcf is not pcf:
            self.components = {
                component_symbol(c): c for c in pcf.components if not c.is_cash_placeholder and c.quantity > 0
                and _normalize_substitution_flag(c.substitution_flag) is not SubstitutionKind.MANDATORY
            }
            self.pcf = pcf
        target = "limit_up:" if side is Side.CREATE else "limit_down:"
        details, unknown = [], {}
        expected_loss = Decimal(0)
        for symbol, status in self.limits.blocked(now).items():
            component = self.components.get(symbol)
            if component is None:
                continue
            if status.startswith("unknown:"):
                unknown[symbol] = status
                continue
            if not status.startswith(target):
                continue
            # status() inspected the exact quote from this same cache.
            price = self.prices.get(symbol, now, exact=True).close
            quantity = component.quantity * units
            value = price * quantity
            rate = estimate_limit_loss(side, symbol.rsplit(".", 1)[1], value)
            expected_loss += rate.expected_loss
            details.append(dict(symbol=symbol, group=rate.group, limit_status=status, signal_price=price, quantity=quantity,
                                stock_value=value, bucket_min=rate.bucket_min, bucket_max=rate.bucket_max,
                                sample_count=rate.sample_count, used_group_fallback=rate.used_group_fallback,
                                loss_per_100=rate.loss_per_100, estimated_loss=rate.expected_loss))
        addon = expected_loss / basket_value
        return dict(base_threshold=base_threshold, signal_basket_value=basket_value,
                    estimated_limit_loss=expected_loss, limit_loss_threshold_addon=addon,
                    effective_threshold=base_threshold + addon, limit_loss_hit_count=len(details),
                    limit_loss_unknown_count=len(unknown),
                    limit_loss_components=json.dumps(details, ensure_ascii=False, default=str),
                    limit_loss_unknown_components=json.dumps(unknown, ensure_ascii=False))

