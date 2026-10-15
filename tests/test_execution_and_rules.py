from datetime import date, datetime, time, timedelta
from decimal import Decimal
import unittest

from etf_arb.domain import Quote, QuoteStatus, Side
from etf_arb.execution.fill_model import NextQuoteFillModel
from etf_arb.execution.models import ExecutionStatus, Order, OrderSide
from etf_arb.rules import (
    ETFRule,
    InMemoryRuleRepository,
    RuleNotFoundError,
    RuleOverlapError,
    SecurityTradingRule,
)


class ExecutionAndRulesTests(unittest.TestCase):
    def test_next_quote_model_uses_latest_existing_ask_without_extra_slippage(self) -> None:
        submitted = datetime(2025, 1, 2, 9, 30)
        order = Order("o-1", "159338", OrderSide.BUY, 100, submitted)
        older_quote = Quote(
            "159338",
            submitted - timedelta(minutes=1),
            ask=Decimal("1.02"),
            status=QuoteStatus.NORMAL,
        )
        latest_quote = Quote(
            "159338",
            submitted,
            ask=Decimal("1.01"),
            status=QuoteStatus.NORMAL,
        )
        future_quote = Quote(
            "159338",
            submitted + timedelta(minutes=1),
            ask=Decimal("0.99"),
            status=QuoteStatus.NORMAL,
        )
        quotes = (
            future_quote,
            older_quote,
            latest_quote,
        )
        result = NextQuoteFillModel().execute(order, quotes)
        self.assertEqual(result.status, ExecutionStatus.FILLED)
        self.assertEqual(result.fills[0].price, Decimal("1.01"))
        self.assertEqual(result.fills[0].ts, latest_quote.ts)

    def test_sell_uses_best_bid_without_extra_slippage_and_missing_quote_is_no_fill(self) -> None:
        submitted = datetime(2025, 1, 2, 9, 30)
        order = Order("o-2", "159338", OrderSide.SELL, 100, submitted)
        result = NextQuoteFillModel().execute(
            order,
            (
                Quote(
                    "159338",
                    submitted - timedelta(seconds=30),
                    bid=Decimal("2.00"),
                    status=QuoteStatus.NORMAL,
                ),
                Quote(
                    "159338",
                    submitted + timedelta(minutes=1),
                    bid=Decimal("2.50"),
                    status=QuoteStatus.NORMAL,
                ),
            ),
        )
        self.assertEqual(result.fills[0].price, Decimal("2.00"))
        no_fill = NextQuoteFillModel().execute(order, ())
        self.assertEqual(no_fill.status, ExecutionStatus.NO_FILL)

    def test_missing_book_side_does_not_fall_back_to_last_price(self) -> None:
        submitted = datetime(2025, 1, 2, 9, 30)
        order = Order("o-3", "159338", OrderSide.BUY, 100, submitted)
        result = NextQuoteFillModel().execute(
            order,
            (
                Quote(
                    "159338",
                    submitted - timedelta(minutes=1),
                    last=Decimal("2.00"),
                    status=QuoteStatus.NORMAL,
                ),
            ),
        )
        self.assertEqual(result.status, ExecutionStatus.NO_FILL)

    def test_historical_repository_selects_date_and_rejects_overlap(self) -> None:
        first = ETFRule(
            "159338", date(2025, 1, 1), date(2025, 1, 31), True, False,
            creation_cutoff=time(14, 30),
        )
        second = ETFRule("159338", date(2025, 2, 1), None, False, True)
        repository = InMemoryRuleRepository([first, second])
        self.assertIs(repository.get("159338", date(2025, 1, 31)), first)
        self.assertFalse(repository.get("159338", date(2025, 1, 31)).allows(Side.REDEEM, 1))
        self.assertIs(repository.get("159338", date(2025, 2, 1)), second)
        with self.assertRaises(RuleNotFoundError):
            repository.get("159338", date(2024, 12, 31))
        with self.assertRaises(RuleOverlapError):
            repository.add_etf_rule(
                ETFRule("159338", date(2025, 1, 31), date(2025, 2, 5), True, True)
            )

    def test_security_rule_normalizes_decimal_costs(self) -> None:
        rule = SecurityTradingRule(
            "000001.SZ",
            date(2025, 1, 1),
            None,
            True,
            True,
            lot_size=100,
            tick_size="0.01",
            commission_rate="0.0003",
        )
        self.assertEqual(rule.tick_size, Decimal("0.01"))
        self.assertTrue(rule.can_buy())
        self.assertTrue(rule.is_trading_time(time(10, 0)))


if __name__ == "__main__":
    unittest.main()
