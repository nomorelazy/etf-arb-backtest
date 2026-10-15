"""Regression tests for the A-share continuous-auction and IPO/ST rules."""

from datetime import date, time
from decimal import Decimal
import unittest

from etf_arb.rules import (
    AShareBoard, InMemoryRuleRepository, a_share_price_limit_rate,
    is_a_share_rule_trading_time, is_continuous_auction_time,
    make_a_share_security_rule,
)


class AShareRuleTests(unittest.TestCase):
    def test_continuous_auction_boundaries_and_lunch_break(self):
        checks = {
            time(9, 29, 59): False,
            time(9, 30): True,
            time(11, 29, 59): True,
            time(11, 30): False,
            time(12, 59, 59): False,
            time(13, 0): True,
            time(14, 56, 59): True,
            time(14, 57): False,
            time(15, 0): False,
        }
        for at, expected in checks.items():
            with self.subTest(at=at):
                self.assertEqual(is_continuous_auction_time(at), expected)

    def test_board_rates_and_st_effective_date(self):
        before = date(2026, 7, 3)
        after = date(2026, 7, 6)
        expected = {
            AShareBoard.MAIN: "0.10",
            AShareBoard.CHINEXT: "0.20",
            AShareBoard.STAR: "0.20",
            AShareBoard.BSE: "0.30",
        }
        for board, rate in expected.items():
            for is_st in (False, True):
                with self.subTest(board=board, is_st=is_st):
                    self.assertEqual(
                        a_share_price_limit_rate(board, after, is_st=is_st),
                        Decimal(rate),
                    )
        self.assertEqual(
            a_share_price_limit_rate(AShareBoard.MAIN, before, is_st=True),
            Decimal("0.05"),
        )
        self.assertEqual(
            a_share_price_limit_rate(AShareBoard.MAIN, before),
            Decimal("0.10"),
        )

    def test_main_st_boundary_never_leaks_future_rate(self):
        """Historic main-board ST snapshots must resolve to the date's version."""
        old = Decimal("0.05")
        new = Decimal("0.10")
        for on, expected in (
            (date(2026, 7, 3), old),   # last trading day before rule change
            (date(2026, 7, 5), old),   # last calendar day before change
            (date(2026, 7, 6), new),   # first trading day under new rule
            (date(2026, 7, 7), new),
        ):
            with self.subTest(on=on):
                self.assertEqual(
                    a_share_price_limit_rate(AShareBoard.MAIN, on, is_st=True),
                    expected,
                )
                self.assertEqual(
                    a_share_price_limit_rate(AShareBoard.MAIN, on, is_st=False),
                    Decimal("0.10"),
                )
                rule = make_a_share_security_rule(
                    "600000.SH", on, AShareBoard.MAIN, is_st=True
                )
                self.assertEqual(rule.limit_up_rate, expected)
                self.assertEqual(rule.limit_down_rate, expected)
                self.assertEqual(rule.effective_from, on)
                self.assertEqual(rule.effective_to, on)

        old_rule = make_a_share_security_rule(
            "600000.SH", date(2026, 7, 3), AShareBoard.MAIN, is_st=True
        )
        new_rule = make_a_share_security_rule(
            "600000.SH", date(2026, 7, 6), AShareBoard.MAIN, is_st=True
        )
        repo = InMemoryRuleRepository(security_rules=[old_rule, new_rule])
        self.assertEqual(
            repo.get_security_rule("600000.SH", date(2026, 7, 3)).limit_up_rate,
            old,
        )
        self.assertEqual(
            repo.get_security_rule("600000.SH", date(2026, 7, 6)).limit_up_rate,
            new,
        )

    def test_ipo_exempt_days_are_trading_day_ordinals(self):
        on = date(2026, 10, 15)
        for board in (AShareBoard.MAIN, AShareBoard.CHINEXT, AShareBoard.STAR):
            with self.subTest(board=board):
                self.assertIsNone(a_share_price_limit_rate(board, on, ipo_trading_day=1))
                self.assertIsNone(a_share_price_limit_rate(board, on, ipo_trading_day=5))
                self.assertIsNotNone(a_share_price_limit_rate(board, on, ipo_trading_day=6))
        self.assertIsNone(a_share_price_limit_rate(AShareBoard.BSE, on, ipo_trading_day=1))
        self.assertEqual(
            a_share_price_limit_rate(AShareBoard.BSE, on, ipo_trading_day=2),
            Decimal("0.30"),
        )

    def test_daily_snapshot_integrates_with_repository(self):
        on = date(2026, 10, 15)
        rule = make_a_share_security_rule(
            "000001.SZ", on, AShareBoard.MAIN
        )
        repo = InMemoryRuleRepository(security_rules=[rule])
        resolved = repo.get_security_rule("000001.SZ", on)
        self.assertEqual(resolved.limit_up_rate, Decimal("0.10"))
        self.assertTrue(is_a_share_rule_trading_time(resolved, time(10, 0)))
        self.assertFalse(is_a_share_rule_trading_time(resolved, time(12, 0)))
        self.assertFalse(is_a_share_rule_trading_time(resolved, time(14, 57)))

    def test_reject_invalid_inputs(self):
        with self.assertRaises(ValueError):
            a_share_price_limit_rate("unknown", date(2026, 10, 15))
        with self.assertRaises(ValueError):
            a_share_price_limit_rate("main", date(2026, 10, 15), ipo_trading_day=0)


if __name__ == "__main__":
    unittest.main()
