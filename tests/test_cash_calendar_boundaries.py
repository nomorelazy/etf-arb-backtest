from datetime import date, timedelta
from types import SimpleNamespace
import unittest

from etf_arb.rules.errors import RuleNotFoundError
from etf_arb.rules.security_calendar import notice_dates, substitution_deadline


class SessionRules:
    def __init__(self, suspended):
        self.suspended = suspended

    def get_security_rule(self, symbol, day):
        if symbol != "000001.SZ":
            raise RuleNotFoundError(symbol)
        return SimpleNamespace(is_suspended=day in self.suspended)


class PricingDateBoundaryTests(unittest.TestCase):
    def test_second_normal_session_on_twentieth_day_uses_ordinary_close(self):
        days = tuple(date(2026, 1, 1) + timedelta(days=i) for i in range(20))
        rules = SessionRules(days[1:18])
        self.assertEqual(substitution_deadline("000001.SZ", days[0], days[:-1], rules), (None, False))
        self.assertEqual(substitution_deadline("000001.SZ", days[0], days, rules), (days[19], False))
        rules = SessionRules(days[1:19])
        self.assertEqual(substitution_deadline("000001.SZ", days[0], days, rules), (days[19], True))

    def test_exception_notice_follows_exchange_then_uses_settlement_business_days(self):
        days = tuple(date(2026, 1, 1) + timedelta(days=i) for i in range(6))
        self.assertEqual(notice_dates(days[0], days), (days[1], days[4]))
        self.assertEqual(notice_dates(days[0], days, cap_exchange_days=(days[0], days[2])),
                         (days[2], days[5]))
