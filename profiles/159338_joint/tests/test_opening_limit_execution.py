"""Opening-reference policy exercised through the actual signal/execution runner."""

import csv
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal as D
import unittest

import test_full_flow as fixtures
from etf_arb.costs import FeeRates
from etf_arb.data.pcf import FilePCFRepository
from etf_arb.data.snapshot import DailyCloseCache
from etf_arb.rules.limit_reference import OPENING_PROXY_SOURCE
from etf_arb.rules.price_limit_filter import PriceLimitFilter
from test_missing_data import write_market


class OpeningLimitExecutionTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.FullFlowTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        changes = dict(entry_at_signal_time=True, market_split_execution=True,
                       max_trades_per_day=1, fees=FeeRates())
        if hasattr(self.case.config.simulation, "check_creation_feasibility"):
            changes["check_creation_feasibility"] = True
        if hasattr(self.case.config.simulation, "enforce_stock_price_limits"):
            changes["enforce_stock_price_limits"] = True
        self.config = replace(self.case.config, filter_price_limits=True,
                              daily_price_limits_path=None,
                              simulation=replace(self.case.config.simulation, **changes))

    def read(self, name, config=None):
        with ((config or self.config).output_dir / name).open(
                encoding="utf-8-sig", newline="") as stream:
            return list(csv.DictReader(stream))

    def test_first_day_open_proxy_enters_and_reconciles_without_mass_substitution(self):
        runner, summary = self.case.run_model(self.case.market_rows(4, 4), self.config)
        self.assertEqual(summary["opened_count"], 1)
        self.assertEqual(summary["pending_count"], 0)
        self.assertFalse(runner.executor.creation_substitutions)
        first = fixtures.DAYS[0]
        physical, = [r for r in runner.executor.fills if r["purpose"] == "ENTRY"]
        proxy, = [r for r in runner.executor.fills if r["purpose"] == "MANAGER"]
        self.assertEqual(physical["ts"], datetime.combine(first, time(9, 30)))
        self.assertEqual(proxy["ts"], datetime.combine(first, time(9, 31)))
        self.assertEqual(sum((D(r["cash_delta"]) for r in self.read("cashflows.csv")), D(0)),
                         D(summary["final_net_pnl"]))
        self.assertEqual(sum((D(r["total"]) for r in self.read("fees.csv")), D(0)),
                         D(summary["fee"]))
        self.assertTrue(all(len(r) == 6 for r in self.read("portfolio_ledger.csv")))

    def test_no_opening_bar_does_not_take_a_later_reference(self):
        first_open = datetime.combine(fixtures.DAYS[0], time(9, 30))
        rows = [r for r in self.case.market_rows(4, 4)
                if not (r[0] == first_open and r[1] == "000001.SZ")]
        runner, summary = self.case.run_model(rows, replace(self.config, quality_check=False))
        self.assertEqual(summary["opened_count"], 0)
        self.assertFalse(runner.executor.orders)
        self.assertFalse(runner.executor.creation_substitutions)
        self.assertTrue(any("missing_previous_close" in r["component_reasons"]
                            for r in runner.source.rows if r["reason"] == "price_limit_unknown"))

    def test_opening_proxy_touch_filters_both_sides_at_first_eligible_signal(self):
        first = fixtures.DAYS[0]
        for etf_price, side in ((4, "CREATE"), (3, "REDEEM")):
            for close, limit in ((1.1, "limit_up"), (.9, "limit_down")):
                with self.subTest(side=side, limit=limit):
                    rows = [(at, symbol, close if symbol == "000001.SZ"
                             and at.date() == first and at.time() > time(9, 30) else price)
                            for at, symbol, price in self.case.market_rows(etf_price, etf_price)]
                    config = replace(self.config,
                        output_dir=self.case.root / f"{side}-{limit}",
                        simulation=replace(self.config.simulation,
                            trading_sessions=(("09:31", "11:29"), ("13:00", "14:56"))))
                    runner, summary = self.case.run_model(rows, config)
                    self.assertEqual(summary["opened_count"], 0)
                    self.assertFalse(runner.executor.fills)
                    blocks = self.read("price_limit_blocks.csv", config)
                    self.assertTrue(blocks)
                    self.assertTrue(all(f"{limit}:{OPENING_PROXY_SOURCE}" in r["component_reasons"]
                                        for r in blocks))

    def test_sourced_previous_close_blocks_redemption_sells_until_open_on_both_markets(self):
        first = fixtures.DAYS[0]
        path = self.case.root / "verified_prices.csv"
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["previous_date", "symbol", "close", "source_url"])
            writer.writerows([["2024-12-31", symbol, price, "https://example.org/daily"]
                              for symbol, price in (("000001.SZ", 1), ("600000.SH", 2))])
        rows = [(at, symbol, (0.9 if symbol == "000001.SZ" else 1.8)
                 if at.date() == first and symbol != "159338.SZ" and at.time() <= time(9, 31)
                 else price) for at, symbol, price in self.case.market_rows(2.5, 2.5)]
        for filtered in (False, True):
            with self.subTest(filtered=filtered):
                config = replace(self.config, filter_price_limits=filtered, report_previous_closes_path=path,
                                 output_dir=self.case.root / f"verified-{filtered}")
                runner, _ = self.case.run_model(rows, config)
                if filtered:
                    self.assertTrue(any(r["reason"] == "price_limit_touched" for r in runner.source.rows))
                else:
                    applied = runner.executor.trades[0]
                    self.assertEqual(applied.applied_at, datetime.combine(first, time(9, 31)))
                    sold = [r for r in runner.executor.fills if r["side"] == "SELL"]
                    self.assertEqual({(r["symbol"], r["ts"]) for r in sold}, {
                        (symbol, datetime.combine(first, time(9, 32)))
                        for symbol in ("000001.SZ", "600000.SH")})

    def test_proxy_execution_waits_for_price_below_opening_based_upper_limit(self):
        first = fixtures.DAYS[0]
        rows = [(at, symbol, 2.2 if symbol == "600000.SH" and at.date() == first
                 and at.time() == time(9, 31) else 2.1 if symbol == "600000.SH"
                 and at.date() == first and at.time() > time(9, 31) else price)
                for at, symbol, price in self.case.market_rows(4, 4)]
        config = replace(self.config, filter_price_limits=False)
        runner, summary = self.case.run_model(rows, config)
        proxy, = [r for r in runner.executor.fills if r["purpose"] == "MANAGER"]
        self.assertEqual((proxy["ts"], proxy["price"]),
                         (datetime.combine(first, time(9, 32)), D("2.1")))
        attempts = runner.executor.substitution_attempts
        self.assertTrue(any(r["ts"] == datetime.combine(first, time(9, 31))
                            and r["reason"].endswith(f"limit_up:{OPENING_PROXY_SOURCE}")
                            and not r["filled_quantity"] for r in attempts))
        self.assertEqual(sum((D(r["cash_delta"]) for r in self.read("cashflows.csv")), D(0)),
                         D(summary["final_net_pnl"]))

    def test_day_open_preload_cannot_expose_opening_proxy_before_0930(self):
        day = fixtures.DAYS[0]
        rows = self.case.market_rows()
        before = datetime.combine(day, time(9, 29))
        rows.append((before, "000001.SZ", 1.1))
        write_market(self.case.market, rows)
        prices = DailyCloseCache(self.case.market)
        prices.load(day, ("000001.SZ", "600000.SH"))
        pcf = FilePCFRepository(self.case.pcf).get("159338", day)
        limits = PriceLimitFilter(prices, self.case.market)
        limits.on_day_open(pcf, self.case.rules)
        self.assertEqual(limits.bounds["000001.SZ"][1], OPENING_PROXY_SOURCE)
        self.assertEqual(limits.status("000001.SZ", before),
                         "unknown:opening_proxy_not_yet_available")
        self.assertIsNone(limits.status("000001.SZ", datetime.combine(day, time(9, 30))))

