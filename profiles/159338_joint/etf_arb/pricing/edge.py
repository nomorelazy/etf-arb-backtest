"""CREATE/REDEEM 方向性毛价差估算。"""

from __future__ import annotations

from decimal import Decimal

from etf_arb.domain import PCF, Quote, Side
from etf_arb.pricing.basket import BasketValuation
from etf_arb.pricing.errors import PricingDataError


def gross_edge(
    side: Side,
    pcf: PCF,
    etf_quote: Quote,
    basket: BasketValuation,
) -> Decimal:
    """计算一个申赎单位的方向性毛价差。

    PCF 现金调整按资金账户视角记号（流入为正）；未知现金调整时拒绝估值，
    避免将未建模现金项目默认为零。
    """
    if side is Side.CREATE:
        if etf_quote.bid is None:
            raise PricingDataError("ETF bid is required to estimate CREATE proceeds.")
        if pcf.creation_cash_adjustment is None:
            raise PricingDataError("CREATE cash adjustment is unknown.")
        return etf_quote.bid * pcf.creation_unit - basket.buy_cost + pcf.creation_cash_adjustment

    if etf_quote.ask is None:
        raise PricingDataError("ETF ask is required to estimate REDEEM cost.")
    if pcf.redemption_cash_adjustment is None:
        raise PricingDataError("REDEEM cash adjustment is unknown.")
    return basket.sell_proceeds - etf_quote.ask * pcf.creation_unit + pcf.redemption_cash_adjustment
