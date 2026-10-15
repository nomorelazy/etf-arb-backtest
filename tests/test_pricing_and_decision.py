from datetime import date, datetime
from decimal import Decimal
import unittest

from etf_arb.decision.gate import DecisionGate, GateCheck
from etf_arb.domain import PCF, PCFComponent, Quote, QuoteStatus, Side
from etf_arb.pricing import BasketValuation, PricingDataError, gross_edge, value_basket


class PricingAndDecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pcf = PCF(
            etf_code="510300",
            effective_date=date(2025, 1, 2),
            creation_unit=100,
            components=(PCFComponent("600000", 10), PCFComponent("000001", 20)),
            creation_cash_adjustment=Decimal("0"),
            redemption_cash_adjustment=Decimal("0"),
        )
        self.component_quotes = {
            "600000": Quote(
                "600000", datetime(2025, 1, 2, 10),
                bid=Decimal("10"), ask=Decimal("10.1"), status=QuoteStatus.NORMAL,
            ),
            "000001": Quote(
                "000001", datetime(2025, 1, 2, 10),
                bid=Decimal("8"), ask=Decimal("8.1"), status=QuoteStatus.NORMAL,
            ),
        }

    def test_basket_uses_ask_to_buy_and_bid_to_sell(self) -> None:
        value = value_basket(self.pcf, self.component_quotes)
        self.assertEqual(value.buy_cost, Decimal("263.0"))
        self.assertEqual(value.sell_proceeds, Decimal("260"))

    def test_create_and_redeem_use_opposite_etf_sides(self) -> None:
        basket = value_basket(self.pcf, self.component_quotes)
        etf = Quote(
            "510300", datetime(2025, 1, 2, 10),
            bid=Decimal("2.70"), ask=Decimal("2.71"), status=QuoteStatus.NORMAL,
        )
        self.assertEqual(gross_edge(Side.CREATE, self.pcf, etf, basket), Decimal("7.0"))
        self.assertEqual(gross_edge(Side.REDEEM, self.pcf, etf, basket), Decimal("-11.0"))

    def test_unknown_cash_adjustment_is_not_assumed_zero(self) -> None:
        pcf = PCF(
            etf_code="510300",
            effective_date=date(2025, 1, 2),
            creation_unit=100,
            components=(PCFComponent("600000", 10),),
        )
        with self.assertRaises(PricingDataError):
            gross_edge(
                Side.CREATE,
                pcf,
                Quote("510300", datetime(2025, 1, 2), bid=Decimal("2.7")),
                BasketValuation(Decimal("100"), Decimal("99")),
            )

    def test_unified_gate_keeps_environment_failure_reason(self) -> None:
        decision = DecisionGate().evaluate(
            etf_code="510300",
            ts=datetime(2025, 1, 2, 10),
            side=Side.CREATE,
            quantity_units=1,
            gross_edge=Decimal("10"),
            estimated_cost=Decimal("2"),
            min_net_edge=Decimal("5"),
            checks=(GateCheck("creation_open", False, "申购当前未开放"),),
        )
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.estimated_net_edge, Decimal("8"))
        self.assertIn("申购当前未开放", decision.reasons)


if __name__ == "__main__":
    unittest.main()
