"""Requested missing-reference fallback, using real minute-Parquet reads."""

from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import pyarrow as pa
import pyarrow.parquet as pq

from etf_arb.data.snapshot import DailyCloseCache
from etf_arb.report.price_limit_bounds import ReportPriceLimits
from etf_arb.rules.errors import RuleNotFoundError
from etf_arb.rules.limit_reference import OPENING_PROXY_SOURCE


DAY = date(2025, 1, 2)
PRIOR = date(2024, 12, 31)
SYMBOL = "000001.SZ"
D = Decimal


class NoOverrides:
    def get_security_rule(self, code, day):
        raise RuleNotFoundError(code)


class OpeningProxyLimitTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.component = SimpleNamespace(security_code="000001", security_name="样本",
            market_code="102", quantity=D(100), is_cash_placeholder=False)
        self.pcf = SimpleNamespace(effective_date=DAY, pre_trading_day=PRIOR,
                                  components=[self.component])

    def write(self, rows):
        months = {}
        for at, symbol, close in rows:
            months.setdefault(at.strftime("%Y-%m"), []).append(
                {"time": at, "thscode": symbol, "close": float(close)})
        for month, records in months.items():
            folder = self.root / month
            folder.mkdir(exist_ok=True)
            pq.write_table(pa.Table.from_pylist(records), folder / "quotes.parquet")

    def limits(self, rows, official=None):
        self.write(rows)
        prices = DailyCloseCache(self.root)
        prices.load(DAY, [SYMBOL])
        limits = ReportPriceLimits(prices, self.root, official)
        limits.on_day_open(self.pcf, NoOverrides())
        return limits

    def test_missing_prior_uses_exact_open_proxy_and_detects_later_limit(self):
        limits = self.limits([(datetime.combine(DAY, time(9, 30)), SYMBOL, 10),
                              (datetime.combine(DAY, time(9, 35)), SYMBOL, 11)])
        self.assertEqual(limits.bounds[SYMBOL], ((D("11.00"), D("9.00")), OPENING_PROXY_SOURCE))
        self.assertEqual(limits.status(SYMBOL, datetime.combine(DAY, time(9, 35))),
                         "limit_up:" + OPENING_PROXY_SOURCE)

    def test_official_no_limit_and_new_listings_override_adjusted_reference_on_ex_date(self):
        feed = self.root / "adjusted_reference_prices.csv"
        feed.write_text(f"date,symbol,reference_price,source_url\n{DAY},{SYMBOL},10,https://example.org/file\n",encoding="utf-8")
        at = datetime.combine(DAY, time(9, 35))
        rows = [(at, SYMBOL, 10.50)]
        self.component.security_name = "XD*ST样本"
        self.assertEqual(self.limits(rows).status(SYMBOL, at), "limit_up:adjusted_reference_price")
        self.component.security_name = "N样本"
        self.assertEqual(self.limits(rows).status(SYMBOL, at), "unknown:needs_official_bounds")
        official = self.root / "official.csv"
        official.write_text(f"date,symbol,upper_limit,lower_limit,no_limit\n{DAY},{SYMBOL},,,1\n",encoding="utf-8")
        self.assertIsNone(self.limits(rows, official).status(SYMBOL, at))

    def test_valid_prior_close_keeps_existing_bounds(self):
        limits = self.limits([(datetime.combine(PRIOR, time(15)), SYMBOL, 10),
                              (datetime.combine(DAY, time(9, 30)), SYMBOL, 10.5)])
        self.assertEqual(limits.bounds[SYMBOL], ((D("11.00"), D("9.00")), "estimated"))

    def test_official_bounds_and_no_limit_override_proxy(self):
        for no_limit in (False, True):
            with self.subTest(no_limit=no_limit):
                path = self.root / "official.csv"
                path.write_text("date,symbol,upper_limit,lower_limit,no_limit\n"
                    f"2025-01-02,{SYMBOL},11.50,8.50,{str(no_limit).lower()}\n", encoding="utf-8")
                limits = self.limits([(datetime.combine(DAY, time(9, 30)), SYMBOL, 10)], path)
                self.assertEqual(limits.bounds[SYMBOL],
                    ((None, None) if no_limit else (D("11.50"), D("8.50")), "provided"))

    def test_no_future_or_nearest_opening_price(self):
        limits = self.limits([(datetime.combine(DAY, time(9, 29)), SYMBOL, 9),
                              (datetime.combine(DAY, time(9, 31)), SYMBOL, 10),
                              (datetime.combine(DAY, time(9, 35)), SYMBOL, 11)])
        self.assertEqual(limits.bounds[SYMBOL], (None, "missing_previous_close"))
        self.assertEqual(limits.status(SYMBOL, datetime.combine(DAY, time(9, 35))),
                         "unknown:missing_previous_close")

    def test_preloaded_open_is_unavailable_before_0930(self):
        limits = self.limits([(datetime.combine(DAY, time(9, 30)), SYMBOL, 10)])
        self.assertEqual(limits.status(SYMBOL, datetime.combine(DAY, time(9, 29))),
                         "unknown:opening_proxy_not_yet_available")
        self.assertIsNone(limits.status(SYMBOL, datetime.combine(DAY, time(9, 30))))

    def test_proxy_keeps_st_rate_and_tick_rounding(self):
        self.component.security_name = "ST样本"
        limits = self.limits([(datetime.combine(DAY, time(9, 30)), SYMBOL, 10.03)])
        self.assertEqual(limits.bounds[SYMBOL], ((D("10.53"), D("9.53")), OPENING_PROXY_SOURCE))

    def test_special_adjustments_still_require_official_bounds(self):
        self.component.security_name = "XD样本"
        limits = self.limits([(datetime.combine(DAY, time(9, 30)), SYMBOL, 10)])
        self.assertEqual(limits.bounds[SYMBOL], (None, "needs_official_bounds"))

    def test_mixed_symbols_only_fills_missing_reference(self):
        other = "000002.SZ"
        self.pcf.components.append(SimpleNamespace(security_code="000002", security_name="第二只",
            market_code="102", quantity=D(100), is_cash_placeholder=False))
        limits = self.limits([(datetime.combine(PRIOR, time(15)), SYMBOL, 10),
            (datetime.combine(DAY, time(9, 30)), SYMBOL, 10.5),
            (datetime.combine(DAY, time(9, 30)), other, 20)])
        self.assertEqual(limits.bounds[SYMBOL][1], "estimated")
        self.assertEqual(limits.bounds[other], ((D("22.00"), D("18.00")), OPENING_PROXY_SOURCE))


if __name__ == "__main__":
    unittest.main()
