"""以买入和卖出方向分别估算 PCF 篮子价值。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from etf_arb.domain import PCF, Quote
from etf_arb.pricing.errors import PricingDataError


@dataclass(frozen=True)
class BasketValuation:
    buy_cost: Decimal
    sell_proceeds: Decimal


def value_basket(pcf: PCF, quotes: Mapping[str, Quote]) -> BasketValuation:
    """按卖盘估算买入成本、按买盘估算卖出收入，不包含现金调整和费用。"""
    buy_cost = Decimal("0")
    sell_proceeds = Decimal("0")

    for component in pcf.components:
        if component.quantity == 0:
            continue
        quote = quotes.get(component.security_code)
        if quote is None:
            raise PricingDataError(f"Missing quote for PCF component {component.security_code}.")
        if quote.ask is None or quote.bid is None:
            raise PricingDataError(f"Both bid and ask are required for {component.security_code}.")
        buy_cost += quote.ask * component.quantity
        sell_proceeds += quote.bid * component.quantity

    return BasketValuation(buy_cost=buy_cost, sell_proceeds=sell_proceeds)
