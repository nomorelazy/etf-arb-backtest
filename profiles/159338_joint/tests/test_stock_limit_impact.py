import csv
import hashlib
import json
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal as D
import unittest

import test_creation_feasibility as baskets
import test_full_flow as fixtures
from etf_arb.costs import FeeRates
from etf_arb.report.rebuild import read_rows, rebuild_run_report


class StockLimitImpactTests(unittest.TestCase):
    def setUp(self):
        self.case = baskets.CreationFeasibilityTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.config = replace(self.case.config, simulation=replace(self.case.config.simulation,
            fees=FeeRates(), quantity_units=2))
        # Exact test limits on the settlement date permit the specified 14:30 price.
        content = self.case.bounds.read_text(encoding="utf-8")
        content = content.replace("2025-01-06,000001.SZ,1.1,0.9", "2025-01-06,000001.SZ,1.5,0.5")
        self.case.bounds.write_text(content, encoding="utf-8")

    def rows(self, physical=1.1, cross=2.2, etf=4.1, transform=None):
        rows = [(datetime.combine(day, clock), symbol, price)
                for day in fixtures.DAYS for clock in fixtures.CLOCKS
                for symbol, price in [("159338.SZ", etf), ("000001.SZ", physical), ("600000.SH", cross)]]
        rows = [(at, symbol, 2.1 if cross == 2.2 and symbol == "600000.SH"
                 and at.date() == fixtures.DAYS[0] and at.time() == time(9, 32) else price)
                for at, symbol, price in rows]
        return [transform(*r) for r in rows] if transform else rows

    def run_model(self, rows=None, config=None):
        config = config or self.config
        runner, summary = self.case.fixture.run_model(self.rows() if rows is None else rows, config)
        data = json.loads((config.output_dir / "stock_limit_impact.json").read_text(encoding="utf-8"))
        return runner, summary, data

    def test_actual_fees_and_early_fills_are_not_repriced_at_t2(self):
        def transform(at, symbol, price):
            if at.date() == fixtures.DAYS[2]:
                if symbol == "000001.SZ":
                    price = 1.2 if at.time() == time(14, 30) else 1.4 if at.time() == time(15) else price
                if symbol == "600000.SH" and at.time() == time(15):
                    price = 2.4
            return at, symbol, price
        runner, summary, data = self.run_model(self.rows(transform=transform))
        stocks = {r["symbol"]: r for r in data["rows"]}
        self.assertEqual(set(stocks), {"000001.SZ", "600000.SH"})
        sz, sh = stocks["000001.SZ"], stocks["600000.SH"]
        self.assertEqual(D(sz["quantity"]), 200)
        self.assertEqual(D(sz["cash_substitution_pnl"]), D("-20.04"))
        self.assertEqual(D(sh["cash_substitution_pnl"]), D("19.92"))
        self.assertEqual(D(sz["execution_price"]), D("1.2"))
        self.assertEqual(sz["last_execution_at"], "2025-01-06 14:30:00")
        self.assertEqual(sh["first_execution_at"], "2025-01-02 09:32:00")
        self.assertEqual(D(sh["execution_price"]), D("2.1"))
        self.assertEqual(sz["pricing_deadline"], "2025-01-06")  # Thursday + two trading days, over weekend.
        self.assertEqual(D(sz["etf_weight"]), D(220) / D(sz["basket_value"]))
        attribution = read_rows(self.config.output_dir / "trade_attribution.csv")[0]
        self.assertEqual(sum(D(r["cash_substitution_pnl"]) for r in stocks.values()),
                         D(attribution["cash_substitution_pnl"]))
        self.assertEqual(D(attribution["net_pnl"]), D(summary["final_net_pnl"]))
        self.assertEqual(data["coverage"][0]["limit_stock_count"], 2)
        self.assertEqual(len(data["rows"]), 2)
        self.assertEqual(data["selection_basis"], "trade_open")
        self.assertEqual(len(data["events"]), 2)
        self.assertTrue(all(r["stage"] == "signal" and r["ts"] == "2025-01-02 09:30:00"
                            for r in data["events"]))
        report = (self.config.output_dir / "report/report.html").read_text(encoding="utf-8")
        self.assertIn("申购涨停／赎回跌停股票价格影响（不含税费）", report)
        self.assertIn("个股价格变动盈亏（元）", report)
        self.assertNotIn("观察价差", report)
        self.assertNotIn("overflow:auto", report)
        self.assertEqual(read_rows(self.config.output_dir / "stock_limit_impact.csv"),
                         read_rows(self.config.output_dir / "report/stock_limit_impact.csv"))
        self.assertEqual(len(read_rows(self.config.output_dir / "portfolio_ledger.csv")[0]), 6)

    def test_redemption_sign_and_physical_fees_are_separate(self):
        def transform(at, symbol, price):
            if at.date() == fixtures.DAYS[0] and at.time() >= time(9, 31):
                price = 1.9 if symbol == "600000.SH" else 0.95 if symbol == "000001.SZ" else price
            if at.date() == fixtures.DAYS[2] and at.time() == time(15) and symbol == "600000.SH":
                price = 1.4
            return at, symbol, price
        _, _, data = self.run_model(self.rows(physical=0.9, cross=1.8, etf=3, transform=transform))
        stocks = {r["symbol"]: r for r in data["rows"]}
        sh, sz = stocks["600000.SH"], stocks["000001.SZ"]
        self.assertEqual(sh["side"], "REDEEM")
        self.assertEqual(D(sh["cash_substitution_pnl"]), D("19.73"))
        self.assertEqual(D(sh["stock_pnl"]), D("19.73"))
        self.assertIsNone(sz["cash_substitution_pnl"])
        self.assertEqual(D(sz["physical_pnl"]), D("9.86"))
        self.assertEqual(sz["limit_types"], "limit_down")

    def test_partial_manager_fill_plus_deadline_valuation(self):
        config = replace(self.config, simulation=replace(self.config.simulation,
            quantity_units=1, max_shares_per_minute=40))
        def transform(at, symbol, price):
            if symbol == "000001.SZ" and at.date() == fixtures.DAYS[2]:
                price = 1.2 if at.time() == time(14, 30) else 1.4 if at.time() == time(15) else price
            return at, symbol, price
        _, _, data = self.run_model(self.rows(cross=2, transform=transform), config)
        stock, = data["rows"]
        self.assertEqual((stock["filled_quantity"], stock["valued_quantity"]), (40, 60))
        self.assertEqual(D(stock["cash_substitution_pnl"]), D("-22.00"))
        self.assertEqual(D(stock["valuation_price"]), D("1.4"))
        self.assertEqual(stock["pnl_reason"], "filled_and_valued")

    def test_missing_exact_t2_close_does_not_use_nearby_or_future_price(self):
        config = replace(self.config, quality_check=False)
        rows = [r for r in self.rows(physical=1) if not (
            r[1] == "600000.SH" and r[0] == datetime.combine(fixtures.DAYS[2], time(15)))]
        rows += [(datetime.combine(fixtures.DAYS[2], time(14, 59)), "600000.SH", 2.9),
                 (datetime(2025, 1, 7, 15), "600000.SH", 3)]
        _, _, data = self.run_model(rows, config)
        stock, = data["rows"]
        self.assertEqual(D(stock["cash_substitution_pnl"]), D("19.92"))
        self.assertEqual(D(stock["execution_price"]), D("2.1"))
        self.assertIsNone(stock["valuation_price"])
        self.assertNotIn("t2_observation_pnl", stock)

    def test_priced_stock_does_not_imply_paid_or_closed_trade(self):
        config = replace(self.config, trading_days=fixtures.DAYS[:2], settlement_end_date=fixtures.DAYS[1])
        _, summary, data = self.run_model(self.rows(physical=1), config)
        stock, = data["rows"]
        self.assertIsNone(summary["final_net_pnl"])
        self.assertEqual(D(stock["cash_substitution_pnl"]), D("19.92"))
        self.assertIsNone(stock["paid_at"])
        self.assertEqual(D(stock["execution_price"]), D("2.1"))
        self.assertNotIn("t2_observation_pnl", stock)

    def test_unpriced_stock_stays_blank(self):
        config = replace(self.config, trading_days=fixtures.DAYS[:2], settlement_end_date=fixtures.DAYS[1])
        _, _, data = self.run_model(self.rows(cross=2), config)
        stock, = data["rows"]
        self.assertIsNone(stock["cash_substitution_pnl"])
        self.assertEqual(stock["pnl_reason"], "stock_not_fully_priced")

    def test_unrelated_later_touch_does_not_select_an_already_executed_stock(self):
        def transform(at, symbol, price):
            if symbol == "600000.SH" and at.time() in (time(9, 35), time(15)):
                price = 2.2
            return at, symbol, price
        _, _, data = self.run_model(self.rows(physical=1, cross=2, transform=transform))
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["coverage"][0]["limit_stock_count"], 0)
        self.assertEqual(data["coverage"][0]["unknown_stock_count"], 0)

    def test_missing_limit_data_is_not_reported_as_confirmed_no_limits(self):
        self.case.prior_feed.unlink()
        config = replace(self.config, daily_price_limits_path=None, creation_reference_prices_path=None,
            simulation=replace(self.config.simulation, check_creation_feasibility=False))
        rows = self.rows(physical=1, cross=2)
        _, summary, data = self.run_model(rows, config)
        self.assertEqual(summary["opened_count"], 1)
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["coverage"][0]["unknown_stock_count"], 0)
        # With no previous close and no exact 09:30 quote, a later signal cannot supply a reference.
        opening = datetime.combine(fixtures.DAYS[0], time(9, 30))
        missing = [row for row in rows if row[0] != opening or row[1] == "159338.SZ"]
        _, summary, data = self.run_model(missing, replace(config, quality_check=False,
            output_dir=self.case.fixture.root / "without-opening-quotes"))
        self.assertEqual(summary["opened_count"], 1)
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["coverage"][0]["unknown_stock_count"], 2)
        self.assertIn("missing_previous_close", data["coverage"][0]["unknown_details"])

    def test_application_and_proxy_attempt_new_touch_does_not_select_stock(self):
        def transform(at, symbol, price):
            if symbol == "600000.SH" and at.date() == fixtures.DAYS[0] and at.time() == time(9, 31):
                price = 2.2
            return at, symbol, price
        runner, _, data = self.run_model(self.rows(physical=1, cross=2, transform=transform))
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["events"], [])
        self.assertEqual(data["coverage"][0]["limit_stock_count"], 0)
        self.assertTrue(any(r["reason"] == "limit_up:provided" for r in runner.executor.substitution_attempts))

    def test_redemption_sale_minute_touch_does_not_select_stock(self):
        def transform(at, symbol, price):
            if at.date() == fixtures.DAYS[0] and at.time() == time(9, 31):
                price = 1.8 if symbol == "600000.SH" else 0.9 if symbol == "000001.SZ" else price
            return at, symbol, price
        runner, _, data = self.run_model(self.rows(physical=1, cross=2, etf=3, transform=transform))
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["events"], [])
        self.assertEqual(data["coverage"][0]["side"], "REDEEM")
        self.assertTrue(any(r["side"] == "SELL" and r["symbol"] == "600000.SH"
                            for r in runner.executor.fills))

    def test_local_limit_up_at_1430_uses_deadline_close_without_fake_fill_or_fee(self):
        def transform(at, symbol, price):
            if symbol == "000001.SZ" and at.date() == fixtures.DAYS[2]:
                price = 1.5 if at.time() == time(14, 30) else 1.4 if at.time() == time(15) else price
            return at, symbol, price
        runner, _, data = self.run_model(self.rows(cross=2, transform=transform))
        stock, = data["rows"]
        self.assertEqual(stock["filled_quantity"], 0)
        self.assertEqual(stock["valued_quantity"], 200)
        self.assertEqual(D(stock["valuation_price"]), D("1.4"))
        self.assertEqual(D(stock["stock_pnl"]), -60)
        self.assertEqual(D(stock["manager_fees"]), 0)
        self.assertEqual(stock["valuation_at"], "2025-01-06 15:00:00")
        attempt = next(r for r in runner.executor.substitution_attempts if r["symbol"] == "000001.SZ")
        self.assertEqual(attempt["reason"], "limit_up:provided")

    def test_cross_market_retries_through_the_inclusive_deadline(self):
        def transform(at, symbol, price):
            if symbol == "600000.SH":
                price = 2.1 if at == datetime.combine(fixtures.DAYS[2], time(15)) else 2.2
            return at, symbol, price
        _, _, data = self.run_model(self.rows(physical=1, transform=transform))
        stock, = data["rows"]
        self.assertEqual(stock["first_execution_at"], "2025-01-06 15:00:00")
        self.assertEqual(stock["filled_quantity"], 200)
        self.assertEqual(stock["valued_quantity"], 0)
        self.assertEqual(D(stock["stock_pnl"]), D("19.92"))

    def test_all_five_etfs_export_weight_and_actual_substitution(self):
        for code in ("159338", "159516", "512760", "512880", "515880"):
            with self.subTest(code=code):
                self.case.configure(code)
                self.case.config = replace(self.case.config,
                    simulation=replace(self.case.config.simulation, quantity_units=2))
                self.case.run_model()
                data = json.loads((self.case.config.output_dir / "stock_limit_impact.json").read_text(encoding="utf-8"))
                stock, = data["rows"]
                self.assertEqual(stock["symbol"], self.case.physical)
                self.assertEqual(D(stock["quantity"]), 200)
                self.assertGreater(D(stock["etf_weight"]), 0)
                self.assertEqual(D(stock["cash_substitution_pnl"]), 0)

    def test_legacy_rebuild_adds_module_without_modifying_original_results(self):
        self.run_model()
        run = self.config.output_dir
        expected = read_rows(run / "stock_limit_impact.csv")
        snapshot = json.loads((run / "stock_limit_impact.json").read_text(encoding="utf-8"))
        snapshot.update(schema_version=2, rows=[{"stale_future_touch": True}])
        (run / "stock_limit_impact.json").write_text(json.dumps(snapshot), encoding="utf-8")
        hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in run.rglob("*") if p.is_file()}
        output = run / "rebuilt"
        rebuild_run_report(run, output)
        self.assertEqual(read_rows(output / "stock_limit_impact.csv"), expected)
        self.assertEqual(read_rows(output / "trade_attribution.csv"), read_rows(run / "trade_attribution.csv"))
        self.assertEqual(hashes, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in hashes})

    def test_filtered_application_rejection_later_touch_is_excluded(self):
        if not hasattr(self.config, "filter_price_limits"):
            self.skipTest("Desktop-only strict price limit filter")
        config = replace(self.config, filter_price_limits=True)
        def transform(at, symbol, price):
            if symbol == "000001.SZ" and at.date() == fixtures.DAYS[0] and at.time() == time(9, 31):
                price = 1.1
            return at, symbol, price
        runner, _, data = self.run_model(self.rows(physical=1, cross=2, transform=transform), config)
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["events"], [])
        self.assertTrue(any(r["purpose"] == "UNWIND" for r in runner.executor.fills))


if __name__ == "__main__":
    unittest.main()
