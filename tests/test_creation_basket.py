import csv
import json
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import Mock
import unittest
import xml.etree.ElementTree as ET

import test_full_flow as fixtures
from etf_arb.data.pcf import FilePCFRepository
from etf_arb.decision.basket import check_creation_basket
from etf_arb.domain import Side
from etf_arb.execution.plan import build_plan
from etf_arb.rules import SecurityTradingRule
from etf_arb.rules.tradability import security_trade_block


class CreationBasketTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def component(self, code="000001", **changes):
        for path in (self.fixture.pcf / "159338").glob("*.xml"):
            tree = ET.parse(path)
            node = tree.find(f"./Components/Component[UnderlyingSecurityID='{code}']")
            for name, value in changes.items():
                child = node.find(name)
                if value is None:
                    if child is not None:
                        node.remove(child)
                else:
                    if child is None:
                        child = ET.SubElement(node, name)
                    child.text = str(value)
            tree.write(path, encoding="utf-8")

    def block_buy(self, code="000001.SZ", *, recover=False, suspended=False):
        end = fixtures.DAYS[0] if recover else fixtures.DAYS[-1]
        self.fixture.rules.add(SecurityTradingRule(code, fixtures.DAYS[0], end, False, True,
                                                  is_suspended=suspended))
        if recover:
            self.fixture.rules.add(SecurityTradingRule(code, fixtures.DAYS[1], fixtures.DAYS[-1], True, True))

    def test_forbidden_and_unbuyable_blocks_signal_before_opening_orders(self):
        self.component(SubstituteFlag="0")
        self.block_buy()
        runner, summary = self.fixture.run_model()
        self.assertEqual(summary["opened_count"], 0)
        self.assertEqual(runner.executor.orders, [])
        self.assertEqual(runner.executor.trades, [])
        blocked = [row for row in runner.source.rows if row["reason"] == "unbuyable_and_no_cash_substitution"]
        self.assertTrue(blocked)
        self.assertEqual(blocked[0]["blocked_components"], "000001.SZ")
        self.assertIn("security_rule_blocked", blocked[0]["component_reasons"])
        with (self.fixture.config.output_dir / "signals.csv").open(encoding="utf-8-sig", newline="") as handle:
            self.assertTrue(any(row["blocked_components"] == "000001.SZ" for row in csv.DictReader(handle)))

    def test_supplemental_prior_blocks_entry_and_drives_reported_settlement(self):
        feed = self.fixture.market / "report_prior_closes.csv"
        feed.write_text("previous_date,symbol,close,source_url\n"
                        "2024-12-31,000001.SZ,0.91,https://example.test/prior\n", encoding="utf-8")
        settings = replace(self.fixture.config.simulation, entry_at_signal_time=True,
                           market_split_execution=True, max_trades_per_day=1)
        config = replace(self.fixture.config, simulation=settings)
        at = datetime.combine(fixtures.DAYS[2], time(14, 30))
        for price, method, value in ((1.05, "manager_scheduled_close_proxy", "1.05"),
                                         (1.10, "unfilled_quantity_at_deadline_close", "1.03")):
            with self.subTest(method=method):
                config = replace(config, output_dir=self.fixture.root / method)
                deadline = at.replace(hour=15, minute=0)
                rows = [(ts, code, price if code == "000001.SZ" and ts == at else
                         1.03 if code == "000001.SZ" and ts == deadline else p)
                        for ts, code, p in self.fixture.market_rows()]
                runner, summary = self.fixture.run_model(rows, config)
                sim = runner.executor
                self.assertEqual(summary["settled_count"], 1)
                self.assertEqual(sim.creation_substitutions[0]["reason"], "limit_up:estimated")
                fills = [r for r in sim.fills if r["symbol"] == "000001.SZ"]
                self.assertFalse(any(r["purpose"] == "ENTRY" for r in fills))
                if price == 1.05:
                    self.assertEqual([(r["ts"], r["price"]) for r in fills], [(at, D(value))])
                else:
                    self.assertEqual(fills, [])
                    self.assertEqual(sim.substitution_pricing[0]["price"], D(value))
                with (config.output_dir / "stock_limit_impact.csv").open(encoding="utf-8-sig") as stream:
                    stock = next(r for r in csv.DictReader(stream) if r["symbol"] == "000001.SZ")
                self.assertEqual((stock["limit_types"], stock["pricing_method"]), ("limit_up", method))
                self.assertEqual(D(stock["stock_pnl"]), (D("1") - D(value)) * 100)
                snapshot = json.loads((config.output_dir / "limit_value.json").read_text(encoding="utf-8"))
                hit = snapshot["rows"][0]["hit_stocks"][0]
                self.assertEqual((hit["symbol"], hit["reference_source"]),
                                 ("000001.SZ", "verified_historical_previous_close_feed"))
                self.assertEqual(sim.account.cash - config.starting_cash, D(summary["final_net_pnl"]))

    def test_ex_rights_day_reference_overrides_actual_previous_close_for_orders_and_report(self):
        feed = self.fixture.market / "adjusted_reference_prices.csv"
        feed.write_text("date,symbol,reference_price,source_url\n"
                        "2025-01-02,000001.SZ,0.91,https://example.test/ex-rights\n")
        settings = replace(self.fixture.config.simulation, entry_at_signal_time=True,
                           market_split_execution=True, max_trades_per_day=1)
        config = replace(self.fixture.config, simulation=settings)
        at = datetime.combine(fixtures.DAYS[2], time(14, 30))
        rows = [(ts, code, 1.05 if code == "000001.SZ" and ts == at else price)
                for ts, code, price in self.fixture.market_rows()]
        rows.append((datetime(2024, 12, 31, 15), "000001.SZ", 1.45))
        runner, summary = self.fixture.run_model(rows, config)
        sim = runner.executor
        self.assertEqual(summary["settled_count"], 1)
        self.assertEqual(sim.creation_substitutions[0]["reason"], "limit_up:adjusted_reference_price")
        fills = [r for r in sim.fills if r["symbol"] == "000001.SZ"]
        self.assertEqual([(r["ts"], r["price"]) for r in fills], [(at, D("1.05"))])
        snapshot = json.loads((config.output_dir / "limit_value.json").read_text(encoding="utf-8"))
        evidence = snapshot["rows"][0]["hit_stocks"][0]
        self.assertEqual((evidence["reference_price"], evidence["reference_source"], evidence["reference_url"]),
                         ("0.91", "adjusted_reference_price", "https://example.test/ex-rights"))
        self.assertEqual(sim.account.cash - config.starting_cash, D(summary["final_net_pnl"]))

    def test_cross_market_creation_waits_for_first_buyable_proxy_minute(self):
        config = self.fixture.config
        bounds = self.fixture.root / "limits.csv"
        bounds.write_text("date,symbol,upper_limit,lower_limit\n2025-01-02,600000.SH,2.20,1.80\n")
        config = replace(config, daily_price_limits_path=bounds, simulation=replace(config.simulation,
                         entry_at_signal_time=True, market_split_execution=True, max_trades_per_day=1))
        rows = [(ts, code, 2.20 if ts.time() == time(9, 31) and code == "600000.SH" else price)
                for ts, code, price in self.fixture.market_rows()]
        runner, _ = self.fixture.run_model(rows, config)
        fills = [r for r in runner.executor.fills if r["symbol"] == "600000.SH"]
        self.assertEqual([(r["ts"], r["price"]) for r in fills],
                         [(datetime.combine(fixtures.DAYS[0], time(9, 32)), D(2))])

    def test_forbidden_but_buyable_keeps_physical_basket_and_pnl(self):
        self.component(SubstituteFlag="0")
        runner, summary = self.fixture.run_model()
        self.assertEqual(summary["opened_count"], 1)
        self.assertEqual(D(summary["final_net_pnl"]), D("48"))
        self.assertEqual(runner.executor.trades[0].plan.physical, {"000001.SZ": 100})
        self.assertEqual(runner.executor.creation_substitutions, [])

    def test_allowed_unbuyable_is_substituted_and_complete_cashflows_reconcile(self):
        self.component(CreationCashSubstitute="0")
        self.block_buy(recover=True)
        runner, summary = self.fixture.run_model()
        trade = runner.executor.trades[0]
        self.assertEqual(trade.plan.physical, {})
        self.assertEqual(trade.plan.manager, {"600000.SH": 100, "000001.SZ": 100})
        self.assertEqual(trade.plan.substitution_deposit, D("363"))
        self.assertEqual(trade.legs[1].final, D("33"))
        self.assertEqual(trade.state, "CLOSED")
        self.assertEqual(D(summary["final_net_pnl"]), D("48"))
        self.assertEqual(sum((r["cash_delta"] for r in runner.executor.account.rows), D("0")), D("48"))
        own_fills = [f for f in runner.executor.fills if f["purpose"] != "MANAGER"]
        self.assertEqual({f["symbol"] for f in own_fills}, {"159338.SZ"})
        manager = next(f for f in runner.executor.fills if f["symbol"] == "000001.SZ")
        self.assertEqual(manager["ts"], datetime.combine(fixtures.DAYS[-1], time(14, 30)))
        route = runner.executor.creation_substitutions[0]
        self.assertEqual(route["deposit"], D("110"))
        self.assertEqual(route["price"], D("1"))
        self.assertEqual(route["quantity"], 100)
        self.assertTrue((self.fixture.config.output_dir / "creation_substitutions.csv").is_file())
        html = (self.fixture.config.output_dir / "report/ledger.html").read_text(encoding="utf-8")
        self.assertIn("深市成分股买入转现金替代", html)
        self.assertIn("signal_minute_close_plus_pcf_premium", html)

    def test_zero_quantity_forbidden_suspended_component_does_not_block(self):
        self.component("000002", SubstituteFlag="0", ComponentShare="0")
        self.block_buy("000002.SZ", suspended=True)
        runner, summary = self.fixture.run_model()
        self.assertEqual(summary["opened_count"], 1)
        self.assertFalse(any("000002.SZ" in row["blocked_components"] for row in runner.source.rows))

    def test_mandatory_and_aggregate_cash_do_not_require_buyability(self):
        self.component(ComponentShare="100", SubstituteFlag="2", CreationCashSubstitute="110", RedemptionCashSubstitute="110")
        self.block_buy()
        runner, summary = self.fixture.run_model()
        self.assertEqual(summary["opened_count"], 1)
        self.assertEqual(runner.executor.trades[0].plan.domestic_fixed, D("130"))
        self.assertEqual(runner.executor.creation_substitutions, [])

    def test_buy_prohibition_does_not_disable_redeem_signal_or_sale(self):
        self.component(SubstituteFlag="0")
        self.block_buy()
        runner, summary = self.fixture.run_model(self.fixture.market_rows(3.2, 3.1))
        self.assertEqual(summary["opened_count"], 1)
        self.assertEqual(runner.executor.trades[0].decision.side, Side.REDEEM)
        self.assertEqual(D(summary["final_net_pnl"]), D("52"))
        self.assertTrue(any(f["symbol"] == "000001.SZ" and f["side"] == "SELL" for f in runner.executor.fills))

    def test_missing_premium_does_not_turn_optional_substitution_into_free_cash(self):
        self.component(PremiumRatio=None, CreationCashSubstitute="0")
        self.block_buy()
        runner, summary = self.fixture.run_model()
        self.assertEqual(summary["opened_count"], 0)
        self.assertEqual(runner.executor.fills, [])
        self.assertTrue(any("missing_creation_premium_rate" in row["component_reasons"] for row in runner.source.rows))

    def test_substitution_scales_deposit_and_quantity_once(self):
        self.block_buy(recover=True)
        config = replace(self.fixture.config, simulation=replace(self.fixture.config.simulation, quantity_units=2))
        runner, summary = self.fixture.run_model(config=config)
        plan = runner.executor.trades[0].plan
        self.assertEqual(plan.substitution_deposit, D("726"))
        self.assertEqual(plan.optional_creation_substitutions, {"000001.SZ": D("220")})
        self.assertEqual(plan.manager["000001.SZ"], 200)
        self.assertEqual(D(summary["final_net_pnl"]), D("96"))

    def test_signal_quote_is_required_and_future_bars_are_not_used(self):
        self.block_buy()
        pcf = FilePCFRepository(self.fixture.pcf).get("159338", fixtures.DAYS[0])
        cache = Mock()
        cache.get.return_value = None
        now = datetime.combine(fixtures.DAYS[0], time(9, 35))
        check = check_creation_basket(pcf, cache, self.fixture.rules, now, "assume_tradable")
        self.assertEqual(check.data_errors, {"000001.SZ": "missing_substitution_deposit_close"})
        self.assertEqual(check.cash_substitutions, {})
        cache.get.assert_called_once_with("000001.SZ", now, exact=True)

    def test_rule_history_gap_and_bare_code_are_respected(self):
        self.block_buy("000001")
        now = datetime.combine(fixtures.DAYS[0], time(9, 35))
        self.assertEqual(security_trade_block(self.fixture.rules, "000001.SZ", now, "BUY", "assume_tradable"), "security_rule_blocked")
        self.assertEqual(security_trade_block(self.fixture.rules, "000001.SZ", now, "SELL", "assume_tradable"), "")
        later = datetime(2025, 1, 7, 9, 35)
        self.assertEqual(security_trade_block(self.fixture.rules, "000001.SZ", later, "BUY", "assume_tradable"), "missing_dated_security_rule")

    def test_execution_rechecks_a_decision_that_bypasses_signal_source(self):
        self.component(SubstituteFlag="0")
        self.block_buy()
        runner, _ = self.fixture.run_model()
        pcf = FilePCFRepository(self.fixture.pcf).get("159338", fixtures.DAYS[0])
        now = datetime.combine(fixtures.DAYS[0], time(9, 30))
        runner.executor.prices.load(fixtures.DAYS[0], {"000001.SZ"})
        context = SimpleNamespace(now=now, pcfs={"159338": pcf}, resources=runner.resources)
        decision = SimpleNamespace(etf_code="159338", side=Side.CREATE, quantity_units=1)
        runner.executor.handle(decision, context)
        self.assertEqual(runner.executor.trades, [])
        self.assertIn("unbuyable_and_no_cash_substitution", runner.executor.rejections[-1]["reason"])

    def test_plan_refuses_cash_substitution_of_a_forbidden_stock(self):
        self.component(SubstituteFlag="0")
        pcf = FilePCFRepository(self.fixture.pcf).get("159338", fixtures.DAYS[0])
        with self.assertRaisesRegex(ValueError, "Cash substitution forbidden"):
            build_plan(pcf, Side.CREATE, 1, creation_cash_substitutions={"000001.SZ": D("110")})


if __name__ == "__main__":
    unittest.main()
