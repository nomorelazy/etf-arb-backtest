"""A-share delisting-period, ST and full-day suspension regression tests."""

from datetime import date, datetime, timedelta
from decimal import Decimal
import unittest

from etf_arb.domain import Quote, QuoteStatus
from etf_arb.execution.fill_model import NextQuoteFillModel
from etf_arb.execution.models import ExecutionStatus, Order, OrderSide
from etf_arb.rules import (
    AShareBoard,
    DELISTING_MAX_SUSPENDED_TRADING_DAYS,
    DELISTING_PERIOD_TRADING_DAYS,
    InMemoryRuleRepository,
    a_share_price_limit_rate,
    delisting_trading_day_index,
    is_a_share_rule_trading_time,
    make_a_share_security_rule,
)


def sample_exchange_days(count: int) -> tuple[date, ...]:
    """Synthetic exchange calendar for tests (not an official holiday calendar)."""
    days = []
    current = date(2026, 10, 15)
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return tuple(days)


class DelistingRuleTests(unittest.TestCase):
    def test_explicit_flags_and_delisting_first_day_no_limit(self):
        for board, expected in (
            (AShareBoard.MAIN, "0.10"),
            (AShareBoard.CHINEXT, "0.20"),
            (AShareBoard.STAR, "0.20"),
            (AShareBoard.BSE, "0.30"),
        ):
            with self.subTest(board=board):
                on = date(2026, 7, 3)  # Before main-board ST uplift
                first = make_a_share_security_rule(
                    "600001.SH", on, board,
                    is_st=True, is_delisting_period=True, delisting_trading_day=1,
                )
                self.assertTrue(first.is_st)
                self.assertTrue(first.is_delisting_period)
                self.assertFalse(first.is_suspended)
                self.assertEqual(first.delisting_trading_day, 1)
                self.assertIsNone(first.limit_up_rate)
                self.assertIsNone(first.limit_down_rate)
                self.assertTrue(first.can_buy())
                self.assertTrue(first.can_sell())
                for nth in (2, 15):
                    rule = make_a_share_security_rule(
                        "600001.SH", on, board,
                        is_st=True, is_delisting_period=True,
                        delisting_trading_day=nth,
                    )
                    # Delisting priority overrides historic ST 5% cap.
                    self.assertEqual(rule.limit_up_rate, Decimal(expected))
                    self.assertEqual(rule.limit_down_rate, Decimal(expected))
                    self.assertTrue(rule.can_buy())

    def test_15_eligible_days_exclude_whole_day_suspensions(self):
        days = sample_exchange_days(18)
        suspended = (days[2], days[7])
        self.assertEqual(DELISTING_PERIOD_TRADING_DAYS, 15)
        self.assertEqual(DELISTING_MAX_SUSPENDED_TRADING_DAYS, 5)
        self.assertEqual(
            delisting_trading_day_index(days[0], days, suspended_days=suspended), 1
        )
        self.assertIsNone(
            delisting_trading_day_index(days[2], days, suspended_days=suspended)
        )
        self.assertEqual(
            delisting_trading_day_index(days[3], days, suspended_days=suspended), 3
        )
        self.assertEqual(
            delisting_trading_day_index(days[16], days, suspended_days=suspended), 15
        )
        self.assertEqual(
            delisting_trading_day_index(days[17], days, suspended_days=suspended), 16
        )
        self.assertIsNone(
            delisting_trading_day_index(
                date(2026, 10, 17), days, suspended_days=suspended
            )
        )

    def test_suspended_day_disallows_both_sides_and_resumes_later(self):
        day = date(2026, 10, 20)
        paused = make_a_share_security_rule(
            "600001.SH", day, AShareBoard.MAIN,
            is_st=True, is_delisting_period=True, is_suspended=True,
        )
        self.assertTrue(paused.is_suspended)
        self.assertTrue(paused.is_st)
        self.assertTrue(paused.is_delisting_period)
        self.assertIsNone(paused.delisting_trading_day)
        self.assertFalse(paused.can_buy())
        self.assertFalse(paused.can_sell())
        self.assertFalse(is_a_share_rule_trading_time(paused, datetime(2026, 10, 20, 10, 0)))

        resumed = make_a_share_security_rule(
            "600001.SH", date(2026, 10, 21), AShareBoard.MAIN,
            is_delisting_period=True, delisting_trading_day=3,
        )
        self.assertTrue(resumed.can_buy())
        self.assertTrue(resumed.can_sell())
        self.assertTrue(is_a_share_rule_trading_time(resumed, datetime(2026, 10, 21, 10, 0)))

        ordinary_suspended = make_a_share_security_rule(
            "000001.SZ", day, AShareBoard.MAIN, is_suspended=True
        )
        self.assertFalse(ordinary_suspended.can_buy())
        self.assertFalse(ordinary_suspended.can_sell())

    def test_day_16_is_not_tradable_and_rule_repo_retains_flags(self):
        on = date(2026, 10, 21)
        expired = make_a_share_security_rule(
            "600001.SH", on, AShareBoard.MAIN,
            is_delisting_period=True, delisting_trading_day=16,
        )
        self.assertFalse(expired.can_buy())
        self.assertFalse(expired.can_sell())
        self.assertFalse(is_a_share_rule_trading_time(expired, datetime(2026, 10, 21, 10, 0)))
        repository = InMemoryRuleRepository(security_rules=[expired])
        loaded = repository.get_security_rule("600001.SH", on)
        self.assertTrue(loaded.is_delisting_period)
        self.assertEqual(loaded.delisting_trading_day, 16)

    def test_invalid_status_and_calendar_inputs_are_rejected(self):
        day = date(2026, 10, 15)
        with self.assertRaises(ValueError):
            a_share_price_limit_rate(
                AShareBoard.MAIN, day, is_delisting_period=True
            )
        with self.assertRaises(ValueError):
            a_share_price_limit_rate(
                AShareBoard.MAIN, day, delisting_trading_day=1
            )
        with self.assertRaises(ValueError):
            a_share_price_limit_rate(
                AShareBoard.MAIN, day,
                is_delisting_period=True, delisting_trading_day=1,
                is_suspended=True,
            )
        with self.assertRaises(ValueError):
            a_share_price_limit_rate(
                AShareBoard.MAIN, day,
                is_delisting_period=True, delisting_trading_day=1,
                ipo_trading_day=1,
            )
        days = sample_exchange_days(10)
        with self.assertRaises(ValueError):
            delisting_trading_day_index(days[-1], days, suspended_days=days[:6])
        with self.assertRaises(ValueError):
            delisting_trading_day_index(days[-1], days[::-1])
        with self.assertRaises(ValueError):
            delisting_trading_day_index(
                days[-1], days, suspended_days=(date(2026, 10, 14),)
            )

    def test_fills_respect_halt_rules_and_expired_delisting(self):
        on = date(2026, 10, 20)
        quote = Quote(
            "600001.SH", datetime(2026, 10, 20, 10, 0),
            ask=Decimal("1.50"), bid=Decimal("1.49"),
            status=QuoteStatus.NORMAL,
        )
        for is_suspended, ordinal in ((True, None), (False, 16)):
            with self.subTest(is_suspended=is_suspended, ordinal=ordinal):
                rule = make_a_share_security_rule(
                    "600001.SH", on, AShareBoard.MAIN,
                    is_delisting_period=True,
                    is_suspended=is_suspended, delisting_trading_day=ordinal,
                )
                model = NextQuoteFillModel(
                    security_rules=InMemoryRuleRepository(security_rules=[rule])
                )
                for side in (OrderSide.BUY, OrderSide.SELL):
                    order = Order(
                        "no-fill", "600001.SH", side, 100,
                        datetime(2026, 10, 20, 10, 0),
                    )
                    result = model.execute(order, (quote,))
                    self.assertEqual(result.status, ExecutionStatus.REJECTED)
                    self.assertFalse(result.fills)

    def test_latest_suspended_quote_blocks_old_normal_quote(self):
        at = datetime(2026, 10, 20, 10, 0)
        order = Order("halt", "600001.SH", OrderSide.BUY, 100, at)
        result = NextQuoteFillModel().execute(
            order,
            (
                Quote(
                    "600001.SH", at - timedelta(minutes=1),
                    ask=Decimal("2"), status=QuoteStatus.NORMAL,
                ),
                Quote(
                    "600001.SH", at,
                    status=QuoteStatus.SUSPENDED,
                ),
            ),
        )
        self.assertEqual(result.status, ExecutionStatus.NO_FILL)
        self.assertFalse(result.fills)


if __name__ == "__main__":
    unittest.main()
