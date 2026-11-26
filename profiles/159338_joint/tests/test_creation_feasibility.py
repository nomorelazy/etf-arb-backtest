import csv
import json
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal as D
from types import SimpleNamespace
import unittest
import xml.etree.ElementTree as ET

import test_full_flow as fixtures
from etf_arb.data.pcf import FilePCFRepository
from etf_arb.domain import Side
from etf_arb.rules import ETFRule, InMemoryRuleRepository
from etf_arb.runner_config import RunnerConfig
from etf_arb.simulation_config import SimulationConfig
from test_missing_data import write_market


class CreationFeasibilityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.prior_feed = self.fixture.write_prior_closes(("000001.SZ", "000002.SZ"))
        self.code = "159338"
        self.physical, self.cross = "000001.SZ", "600000.SH"
        self.bounds = self.fixture.root / "bounds.csv"
        self.refs = self.fixture.root / "references.csv"
        self.config = replace(self.fixture.config,
            daily_price_limits_path=self.bounds, creation_reference_prices_path=self.refs,
            simulation=replace(self.fixture.config.simulation,
                check_creation_feasibility=True, entry_at_signal_time=True, market_split_execution=True))
        self.configure()

    def configure(self, code="159338", cap="0.25", flag="1"):
        self.code = code
        sh = code.startswith("5")
        self.physical, self.cross = (("600000.SH", "000001.SZ") if sh
                                    else ("000001.SZ", "600000.SH"))
        folder = self.fixture.pcf / code
        folder.mkdir(exist_ok=True)
        for day in fixtures.DAYS:
            if sh:
                payload = {
                    "info": [["date", day.strftime("%Y%m%d"), "TradingDay"]],
                    "t-1": {"properties": [["cash", "12", "CashComponent"],
                        ["previous", (fixtures.DAYS[fixtures.DAYS.index(day)-1] if day != fixtures.DAYS[0]
                                      else fixtures.DAYS[0].replace(year=2024, month=12, day=31)).strftime("%Y%m%d"), "PreTradingDay"]]},
                    "t": {"properties": [["cash", "10", "EstimateCashComponent"],
                        ["unit", "100", "CreationRedemptionUnit"], ["switch", "1", "CreationRedemption"],
                        ["cap", cap, "MaxCashRatio"]]},
                    "securities": {"rows": [
                        ["600000", "physical", "100", flag, "10%", "0%", ""],
                        ["000001", "proxy", "100", "深市退补", "10%", "0%", "200"],
                        ["600001", "fixed", "100", "必须", "", "", "20"]]},
                }
                (folder / f"{day:%Y%m%d}.json").write_text(json.dumps(payload), encoding="utf-8")
            else:
                source = self.fixture.pcf / "159338" / f"{day:%Y%m%d}.xml"
                tree = ET.parse(source)
                tree.find("SecurityID").text = code
                node = tree.find("MaxCashRatio")
                if node is None:
                    node = ET.SubElement(tree.getroot(), "MaxCashRatio")
                node.text = cap
                tree.find("./Components/Component[UnderlyingSecurityID='000001']/SubstituteFlag").text = flag
                tree.write(folder / f"{day:%Y%m%d}.xml", encoding="utf-8")
        suffix = "SH" if sh else "SZ"
        self.config = replace(self.config, etf_codes=(code,), output_dir=self.fixture.root / f"run-{code}")
        self.fixture.rules = InMemoryRuleRepository([
            ETFRule(code, fixtures.DAYS[0], fixtures.DAYS[-1], True, True, time(14, 55), time(14, 55))])
        with self.bounds.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["date", "symbol", "upper_limit", "lower_limit"])
            for day in fixtures.DAYS:
                writer.writerow([day, self.physical, "1.1", "0.9"])
                writer.writerow([day, self.cross, "2.2", "1.8"])
        with self.refs.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["date", "symbol", "reference_price"])
            for day in fixtures.DAYS:
                writer.writerows([[day, self.physical, 1], [day, self.cross, 2], [day, f"{code}.{suffix}", 4]])

    def run_model(self, physical=1.1, cross=2.0, etf=4.1, extra_rows=()):
        suffix = "SH" if self.code.startswith("5") else "SZ"
        rows = [(datetime.combine(day, clock), symbol, price)
                for day in fixtures.DAYS for clock in fixtures.CLOCKS
                for symbol, price in [(f"{self.code}.{suffix}", etf),
                                      (self.physical, physical), (self.cross, cross)]]
        rows.extend(extra_rows)
        return self.fixture.run_model(rows, self.config)

    def test_official_stock_suspension_allows_cash_basket_and_never_fills_through_halt(self):
        first = fixtures.DAYS[0]
        root = self.fixture.market
        root.mkdir(exist_ok=True)
        feed = root / "stock_suspensions.csv"
        with feed.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(("symbol", "start_date", "end_date", "source_url"))
            writer.writerow((self.physical, first, fixtures.DAYS[2], "https://example.org/notice"))
        self.config = replace(self.config, filter_price_limits=True)
        runner, summary = self.run_model(physical=1)
        self.assertEqual(summary["opened_count"], 1)
        self.assertEqual(runner.executor.creation_substitutions[0]["reason"], "stock_suspended")
        self.assertFalse(any(f["symbol"] == self.physical for f in runner.executor.fills + runner.executor.fee_rows))
        self.assertFalse(any(row["symbol"] == self.physical for row in runner.executor.substitution_pricing))
        self.assertEqual(summary["pending_count"], 1)
        self.assertIsNone(summary["final_net_pnl"])

    def test_base_rule_suspension_at_carried_limit_skips_strict_basket_filter(self):
        from etf_arb.rules import SecurityTradingRule

        first = fixtures.DAYS[0]
        self.fixture.rules.add(SecurityTradingRule(
            self.physical, first, first, True, True, is_suspended=True))
        self.config = replace(self.config, filter_price_limits=True)
        runner, summary = self.run_model(physical=1.1)
        self.assertEqual(summary["opened_count"], 1)
        self.assertEqual(runner.executor.creation_substitutions[0]["reason"], "stock_suspended")

    def test_forbidden_limit_up_suppresses_decision_and_all_entry_orders(self):
        self.configure(flag="0")
        runner, summary = self.run_model()
        self.assertEqual(summary["opened_count"], 0)
        self.assertFalse(runner.executor.orders)
        blocked = [r for r in runner.source.rows if r["reason"] == "unbuyable_and_no_cash_substitution"]
        self.assertTrue(blocked)
        self.assertIn("limit_up:provided", blocked[0]["component_reasons"])

    def test_supplement_used_for_unfiltered_orders_cap_and_report(self):
        self.config = replace(self.config, daily_price_limits_path=None, creation_reference_prices_path=None,
                              filter_price_limits=False)
        path = self.fixture.market / "report_prior_closes.csv"
        path.parent.mkdir(exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["previous_date", "symbol", "close", "source_url"])
            for symbol, value in ((self.physical, 1), (self.cross, 2), ("159338.SZ", 4)):
                writer.writerow(["2024-12-31", symbol, value, "https://example.org/daily"])
        runner, summary = self.run_model()
        self.assertEqual(summary["opened_count"], 1)
        trade, = runner.executor.trades
        self.assertEqual(trade.plan.manager[self.physical], 100)
        fills = [row for row in runner.executor.fills if row["symbol"] == self.physical]
        self.assertEqual([row["ts"] for row in fills], [datetime.combine(fixtures.DAYS[2], time(14, 30))])
        signal = next(row for row in runner.source.rows if row["reason"] == "threshold_crossed")
        self.assertEqual(signal["cash_substitution_ratio"], D("0.25"))
        report = (self.config.output_dir / "limit_value.json").read_text(encoding="utf-8")
        self.assertIn("verified_historical_previous_close_feed", report)
        with (self.config.output_dir / "limit_exposure.csv").open(encoding="utf-8-sig", newline="") as f:
            exposure = next(row for row in csv.DictReader(f) if row["symbol"] == self.physical)
        self.assertEqual(exposure["initial_limit_state"], "limit_up")

    def test_adjusted_reference_routes_full_runner_and_invalidates_report_caches(self):
        self.config = replace(self.config, daily_price_limits_path=None, creation_reference_prices_path=None)
        path = self.fixture.market / "adjusted_reference_prices.csv"
        path.parent.mkdir(exist_ok=True)
        first = fixtures.DAYS[0]
        url = "https://example.org/issuer/ex-rights"
        def feed(value):
            path.write_text(f"date,symbol,reference_price,source_url\n"
                            f"{first},{self.physical},{value},{url}\n"
                            f"{first},159338.SZ,4,{url}\n", encoding="utf-8")
        feed(1)
        # Unadjusted 15:00 was 1.40 (old inferred lower 1.26), today's adjusted upper is 1.10.
        prior = datetime(2024, 12, 31, 15)
        runner, summary = self.run_model(physical=1.1, extra_rows=[(prior, self.physical, 1.4)])
        self.assertEqual(summary["opened_count"],1)
        trade, = runner.executor.trades
        self.assertIn(self.physical, trade.plan.manager)
        self.assertFalse(any(f["purpose"] == "ENTRY" and f["symbol"] == self.physical for f in runner.executor.fills))
        signal = next(row for row in runner.source.rows if row["reason"] == "threshold_crossed")
        self.assertEqual(signal["cash_substitution_ratio"], D(".25"))
        self.assertEqual(signal["cash_substitution_reference_basis"], "adjusted_reference_price")
        report = json.loads((self.config.output_dir / "limit_value.json").read_text(encoding="utf-8"))
        stock = next(s for s in report["rows"][0]["hit_stocks"] if s["symbol"] == self.physical)
        self.assertEqual((stock["reference_source"], D(stock["reference_price"]), stock["reference_url"]),
                         ("adjusted_reference_price",D(1),url))
        from etf_arb.report.prior_closes import prior_closes_match
        self.assertTrue(prior_closes_match(report,self.config))
        feed("1.05")
        self.assertFalse(prior_closes_match(report,self.config))

    def test_all_five_etfs_allow_full_substitution_at_cap_and_scale_units_once(self):
        self.config = replace(self.config, simulation=replace(self.config.simulation, quantity_units=2))
        for code in ("159338", "159516", "512760", "512880", "515880"):
            with self.subTest(code=code):
                self.configure(code)
                runner, summary = self.run_model()
                self.assertEqual(summary["opened_count"], 1)
                trade, = runner.executor.trades
                self.assertNotIn(self.physical, trade.plan.physical)
                self.assertEqual(trade.plan.manager[self.physical], 200)
                expected_deposit = D("220") if code == "159338" else D("242")
                self.assertEqual(trade.plan.optional_creation_substitutions[self.physical], expected_deposit)
                self.assertFalse(any(f["symbol"] == self.physical and f["purpose"] == "ENTRY"
                                     for f in runner.executor.fills))
                signal = next(r for r in runner.source.rows if r["reason"] == "threshold_crossed")
                self.assertEqual(signal["cash_substitution_ratio"], D("0.25"))
                self.assertEqual(signal["cash_substitution_reference_value"], D("100"))
                self.assertEqual(signal["max_cash_substitution_ratio"], D("0.25"))

    def test_over_cap_blocks_signal_and_execution_bypass(self):
        self.configure(cap="0.24999999999999999999")
        runner, summary = self.run_model()
        self.assertEqual(summary["opened_count"], 0)
        self.assertTrue(any(r["reason"] == "creation_cash_ratio_exceeded" for r in runner.source.rows))
        pcf = FilePCFRepository(self.fixture.pcf).get(self.code, fixtures.DAYS[0])
        now = datetime.combine(fixtures.DAYS[0], time(9, 30))
        runner.executor.prices.load(fixtures.DAYS[0], {self.physical, self.cross, "159338.SZ"})
        runner.executor.creation_guard.on_day_open(pcf, self.fixture.rules)
        context = SimpleNamespace(now=now, pcfs={self.code: pcf}, resources=runner.resources)
        runner.executor.handle(SimpleNamespace(etf_code=self.code, side=Side.CREATE, quantity_units=1), context)
        self.assertFalse(runner.executor.orders)
        self.assertIn("creation_cash_ratio_exceeded", runner.executor.rejections[-1]["reason"])
        with (self.config.output_dir / "signals.csv").open(encoding="utf-8-sig", newline="") as f:
            row = next(r for r in csv.DictReader(f) if r["reason"] == "creation_cash_ratio_exceeded")
        self.assertEqual(D(row["cash_substitution_ratio"]), D("0.25"))

    def test_aggregate_optional_value_not_each_stock_separately(self):
        self.configure(cap="0.4")
        for path in (self.fixture.pcf / self.code).glob("*.xml"):
            tree = ET.parse(path)
            node = tree.find("./Components/Component[UnderlyingSecurityID='000002']")
            node.find("ComponentShare").text = "100"
            node.find("SubstituteFlag").text = "1"
            ET.SubElement(node, "PremiumRatio").text = "0.1"
            tree.write(path, encoding="utf-8")
        with self.bounds.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([day, "000002.SZ", "1.1", "0.9"] for day in fixtures.DAYS)
        with self.refs.open("a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([day, "000002.SZ", "1"] for day in fixtures.DAYS)
        rows = [(datetime.combine(day, clock), "000002.SZ", 1.1)
                for day in fixtures.DAYS for clock in fixtures.CLOCKS]
        runner, summary = self.run_model(etf=6, extra_rows=rows)
        self.assertEqual(summary["opened_count"], 0)
        signal = next(r for r in runner.source.rows if r["reason"] == "creation_cash_ratio_exceeded")
        self.assertEqual(signal["cash_substitution_ratio"], D("0.5"))

    def test_cross_market_limit_up_and_fixed_cash_do_not_consume_optional_cap(self):
        self.configure(cap="0")
        runner, summary = self.run_model(physical=1, cross=2.2)
        self.assertEqual(summary["opened_count"], 1)
        self.assertEqual(runner.executor.trades[0].plan.optional_creation_substitutions, {})

    def test_lower_limit_does_not_prevent_buying_physical_stock(self):
        self.configure(cap="0", flag="0")
        runner, summary = self.run_model(physical=0.9)
        self.assertEqual(summary["opened_count"], 1)
        self.assertEqual(runner.executor.trades[0].plan.physical[self.physical], 100)

    def test_redemption_is_not_filtered_by_creation_cap_or_buy_limit(self):
        self.configure(cap="0", flag="0")
        runner, summary = self.run_model(etf=3)
        self.assertEqual(summary["opened_count"], 1)
        self.assertEqual(runner.executor.trades[0].decision.side, Side.REDEEM)

    def test_missing_reference_never_uses_signal_or_future_close(self):
        self.prior_feed.unlink()
        self.config = replace(self.config, creation_reference_prices_path=None)
        runner, summary = self.run_model()
        self.assertEqual(summary["opened_count"], 0)
        self.assertTrue(any("missing_creation_deposit_reference" in r["component_reasons"]
                            for r in runner.source.rows))

    def test_missing_limit_prices_fail_closed_with_distinct_reason(self):
        self.prior_feed.unlink()
        self.config = replace(self.config, daily_price_limits_path=None, quality_check=False)
        rows = [row for row in self.fixture.market_rows(4.1, 4.1) if not (
            row[0] == datetime.combine(fixtures.DAYS[0], time(9, 30))
            and row[1] == self.physical)]
        runner, summary = self.fixture.run_model(rows, self.config)
        self.assertEqual(summary["opened_count"], 0)
        self.assertTrue(any("unknown:missing_previous_close" in r["component_reasons"]
                            for r in runner.source.rows))

    def test_missing_cap_is_not_assumed_to_allow_unlimited_substitution(self):
        for path in (self.fixture.pcf / self.code).glob("*.xml"):
            tree = ET.parse(path)
            tree.getroot().remove(tree.find("MaxCashRatio"))
            tree.write(path, encoding="utf-8")
        runner, summary = self.run_model()
        self.assertEqual(summary["opened_count"], 0)
        self.assertTrue(any("missing_or_invalid_max_cash_ratio" in r["component_reasons"]
                            for r in runner.source.rows))

    def test_reference_fallback_reads_only_the_exact_labelled_prior_close(self):
        previous = fixtures.DAYS[0].replace(year=2024, month=12, day=31)
        path = write_market(self.fixture.market, [
            (datetime.combine(previous, time(14, 59)), self.physical, 0.5),
            (datetime.combine(previous, time(15)), self.physical, 1.0),
            (datetime.combine(previous, time(15)), "159338.SZ", 4.0),
        ], name="prior.parquet")
        directory = self.fixture.market / "2024-12"
        directory.mkdir()
        path.rename(directory / path.name)
        self.config = replace(self.config, creation_reference_prices_path=None)
        runner, summary = self.run_model()
        self.assertEqual(summary["opened_count"], 1)
        signal = next(r for r in runner.source.rows if r["reason"] == "threshold_crossed")
        self.assertEqual(signal["cash_substitution_ratio"], D("0.25"))
        self.assertEqual(signal["cash_substitution_reference_basis"], "previous_close_proxy")
        self.assertEqual(runner.source.creation_guard.previous.prices.times[self.physical],
                         [datetime.combine(previous, time(15))])

    def test_adjusted_reference_is_required_for_known_corporate_action(self):
        for path in (self.fixture.pcf / self.code).glob("*.xml"):
            tree = ET.parse(path)
            node = tree.find("./Components/Component[UnderlyingSecurityID='000001']")
            ET.SubElement(node, "UnderlyingSymbol").text = "XD测试"
            tree.write(path, encoding="utf-8")
        adjusted = self.fixture.market / "adjusted_reference_prices.csv"
        adjusted.write_text("date,symbol,reference_price,source_url\n"
                            "2025-01-02,000001.SZ,1,https://example.test/ex-rights\n", encoding="utf-8")
        for supplied in (True, False):
            with self.subTest(supplied=supplied):
                if not supplied:
                    adjusted.unlink()
                    lines = self.refs.read_text(encoding="utf-8").splitlines()
                    self.refs.write_text("\n".join(line for line in lines if self.physical not in line), encoding="utf-8")
                self.config = replace(self.config, output_dir=self.fixture.root / f"adjusted-{supplied}")
                runner, summary = self.run_model()
                self.assertEqual(summary["opened_count"], 1 if supplied else 0)
                if not supplied:
                    self.assertTrue(any("missing_adjusted_deposit_reference" in r["component_reasons"]
                                        for r in runner.source.rows))

    def test_reference_etf_price_not_signal_price_is_the_denominator(self):
        self.refs.write_text(self.refs.read_text(encoding="utf-8").replace("159338.SZ,4", "159338.SZ,2"), encoding="utf-8")
        runner, summary = self.run_model()
        self.assertEqual(summary["opened_count"], 0)
        signal = next(r for r in runner.source.rows if r["reason"] == "creation_cash_ratio_exceeded")
        self.assertEqual(signal["cash_substitution_ratio"], D("0.5"))

    def test_defaults_and_config_snapshot_keep_guard_and_input_paths(self):
        self.assertTrue(SimulationConfig().check_creation_feasibility)
        for value in (None, 1, "true"):
            with self.assertRaises(ValueError):
                SimulationConfig(check_creation_feasibility=value)
        runner, _ = self.run_model()
        config = RunnerConfig.from_json(self.config.output_dir / "config.json")
        self.assertTrue(config.simulation.check_creation_feasibility)
        self.assertEqual(config.daily_price_limits_path, self.bounds)
        self.assertEqual(config.creation_reference_prices_path, self.refs)
        self.assertIs(runner.source.creation_guard, runner.executor.creation_guard)
        self.assertIn("申购可完成性检查", (self.config.output_dir / "report/report.html").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
