import csv
import json
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal as D
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from etf_arb.data.pcf import FilePCFRepository
from etf_arb.data.minute_close import MinuteCloseConflictError
from etf_arb.data.snapshot import DailyCloseCache
from etf_arb.execution.plan import build_plan
from etf_arb.full_flow import make_full_runner
from etf_arb.portfolio.account import CashAccount
from etf_arb.portfolio.ledger import PortfolioLedger
from etf_arb.rules import ETFRule, InMemoryRuleRepository, SecurityTradingRule
from etf_arb.runner_config import RunnerConfig
from etf_arb.simulation_config import SimulationConfig
from etf_arb.domain import Side
from test_missing_data import write_market


DAYS = (date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6))
CLOCKS = tuple(time(9, minute) for minute in range(30, 36)) + (time(15),)


class FullFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pcf, self.market = self.root / "pcf", self.root / "market"
        self.pcf.mkdir()
        self.market.mkdir()
        self.rules = InMemoryRuleRepository([ETFRule("159338", DAYS[0], DAYS[-1], True, True, time(14, 55), time(14, 55))])
        self.config = RunnerConfig(DAYS[0], DAYS[0], ("159338",), self.pcf, self.market,
                                   self.root / "run", DAYS, starting_cash=D("10000"), mode="execute",
                                   clock_times=CLOCKS, settlement_end_date=DAYS[-1],
                                   simulation=SimulationConfig(cash_substitution_lag=2, security_rule_policy="assume_tradable"))
        self.write_pcfs()

    def write_pcfs(self, wrong_date=False):
        folder = self.pcf / "159338"
        folder.mkdir(exist_ok=True)
        for index, day in enumerate(DAYS):
            previous = DAYS[index-1] if index else date(2024, 12, 31)
            if wrong_date:
                previous = date(2024, 12, 31)
            (folder / f"{day:%Y%m%d}.xml").write_text(f"""<PCFFile>
            <SecurityID>159338</SecurityID><TradingDay>{day:%Y%m%d}</TradingDay>
            <PreTradingDay>{previous:%Y%m%d}</PreTradingDay><CashComponent>12</CashComponent>
            <CreationRedemptionUnit>100</CreationRedemptionUnit><EstimateCashComponent>10</EstimateCashComponent>
            <Creation>1</Creation><Redemption>1</Redemption><CreationLimit>0</CreationLimit>
            <Components>
            <Component><UnderlyingSecurityID>159900</UnderlyingSecurityID><UnderlyingSymbol>申赎现金</UnderlyingSymbol>
            <UnderlyingSecurityIDSource>102</UnderlyingSecurityIDSource><ComponentShare>0</ComponentShare><SubstituteFlag>2</SubstituteFlag>
            <CreationCashSubstitute>253</CreationCashSubstitute><RedemptionCashSubstitute>207</RedemptionCashSubstitute></Component>
            <Component><UnderlyingSecurityID>000001</UnderlyingSecurityID><UnderlyingSecurityIDSource>102</UnderlyingSecurityIDSource>
            <ComponentShare>100</ComponentShare><SubstituteFlag>1</SubstituteFlag></Component>
            <Component><UnderlyingSecurityID>600000</UnderlyingSecurityID><UnderlyingSecurityIDSource>101</UnderlyingSecurityIDSource>
            <ComponentShare>100</ComponentShare><SubstituteFlag>1</SubstituteFlag></Component>
            <Component><UnderlyingSecurityID>000002</UnderlyingSecurityID><UnderlyingSecurityIDSource>102</UnderlyingSecurityIDSource>
            <ComponentShare>0</ComponentShare><SubstituteFlag>2</SubstituteFlag>
            <CreationCashSubstitute>20</CreationCashSubstitute><RedemptionCashSubstitute>20</RedemptionCashSubstitute></Component>
            <Component><UnderlyingSecurityID>600001</UnderlyingSecurityID><UnderlyingSecurityIDSource>101</UnderlyingSecurityIDSource>
            <ComponentShare>0</ComponentShare><SubstituteFlag>2</SubstituteFlag>
            <CreationCashSubstitute>30</CreationCashSubstitute><RedemptionCashSubstitute>30</RedemptionCashSubstitute></Component>
            </Components></PCFFile>""", encoding="utf-8")

    def market_rows(self, first=4.0, later=4.1):
        return [(datetime.combine(day, clock), symbol,
                 (first if clock == CLOCKS[0] else later) if symbol == "159338.SZ" else price)
                for day in DAYS for clock in CLOCKS
                for symbol, price in (("159338.SZ", 0), ("000001.SZ", 1), ("600000.SH", 2))]

    def run_model(self, rows=None, config=None):
        write_market(self.market, self.market_rows() if rows is None else rows)
        runner = make_full_runner(config or self.config, rules=self.rules)
        result = runner.run()
        return runner, result.financial_summary

    def test_create_cashflows_and_future_cash_component_reconcile(self):
        runner, summary = self.run_model()
        self.assertEqual(summary["final_net_pnl"], "48.0")
        self.assertEqual(summary["ending_cash"], "10048.0")
        self.assertEqual(summary["pending_count"], 0)
        self.assertEqual(runner.executor.account.holdings("T000001"), {})
        fills = runner.executor.fills
        self.assertEqual(fills[0]["ts"].time(), time(9, 31))
        self.assertEqual(next(f for f in fills if f["symbol"] == "159338.SZ")["price"], D("4.1"))
        self.assertEqual({f["symbol"] for f in fills if f["purpose"] != "MANAGER"}, {"159338.SZ", "000001.SZ"})
        self.assertEqual({f["fee"] for f in fills}, {D("0")})
        cash = runner.executor.trades[0].legs[0]
        self.assertEqual(cash.final, D("-2"))
        self.assertEqual(cash.known_on, DAYS[1])
        self.assertEqual(cash.due, DAYS[2])
        self.assertTrue((self.config.output_dir / "report/report.html").is_file())
        raw = json.loads((self.config.output_dir / "run_summary.json").read_text())
        self.assertTrue(raw["financial_results_available"])
        self.assertEqual(D(raw["net_pnl"]), D("48"))

    def test_redeem_returns_estimated_cash_deposit_and_actual_component(self):
        runner, summary = self.run_model(self.market_rows(3.2, 3.1))
        self.assertEqual(D(summary["final_net_pnl"]), D("52"))
        self.assertEqual(D(summary["ending_cash"]), D("10052"))
        self.assertEqual(runner.executor.trades[0].legs[0].final, D("22"))
        self.assertEqual(runner.executor.fills[0]["price"], D("3.1"))

    def test_full_cycle_can_reopen_opposite_direction_after_settlement(self):
        rows = [r for r in self.market_rows() if r[0].date() == DAYS[0]]
        rows += [r for r in self.market_rows(3.2, 3.1) if r[0].date() != DAYS[0]]
        settings = replace(self.config.simulation, cash_substitution_lag=0, cash_difference_lag=0)
        runner, summary = self.run_model(rows, replace(self.config, end_date=DAYS[1], simulation=settings))
        self.assertEqual([t.decision.side for t in runner.executor.trades], [Side.CREATE, Side.REDEEM])
        self.assertEqual(summary["settled_count"], 2)
        self.assertEqual(D(summary["final_net_pnl"]), D("100"))
        self.assertEqual(D(summary["ending_cash"]), D("10100"))

    def test_exact_four_percent_in_both_directions_does_not_trigger(self):
        for price in (3.744, 3.456):
            config = replace(self.config, output_dir=self.root / str(price))
            _, summary = self.run_model(self.market_rows(price, price), config)
            self.assertEqual(summary["opened_count"], 0)
            self.assertEqual(D(summary["final_net_pnl"]), D("0"))

    def test_incomplete_horizon_keeps_trade_pending_and_final_pnl_null(self):
        config = replace(self.config, trading_days=DAYS[:2], settlement_end_date=DAYS[1])
        runner, summary = self.run_model(config=config)
        self.assertEqual(summary["pending_count"], 1)
        self.assertIsNone(summary["final_net_pnl"])
        self.assertEqual(D(summary["settled_net_pnl"]), D("0"))
        with self.assertRaises(ValueError):
            runner.resources.portfolio.total_net_pnl(DAYS[0], DAYS[0])

    def test_mismatched_pretradingday_never_settles_cash_difference(self):
        self.write_pcfs(wrong_date=True)
        runner, summary = self.run_model()
        self.assertEqual(summary["pending_count"], 1)
        self.assertIsNone(runner.executor.trades[0].legs[0].final)

    def test_insufficient_cash_rejects_before_opening_trade(self):
        runner, summary = self.run_model(config=replace(self.config, starting_cash=D("100")))
        self.assertEqual(summary["opened_count"], 0)
        self.assertTrue(summary["rejected_decision_count"])
        self.assertEqual(runner.executor.account.cash, D("100"))
        self.assertFalse(runner.executor.fills)

    def test_no_missing_bar_fill_and_exit_holds_to_next_day(self):
        rows = [r for r in self.market_rows() if not (
            r[1] == "159338.SZ" and r[0].date() == DAYS[0] and r[0].time() >= time(9, 32))]
        runner, summary = self.run_model(rows, replace(self.config, quality_check=False))
        exit_fill = next(f for f in runner.executor.fills if f["purpose"] == "EXIT")
        self.assertEqual(exit_fill["ts"].date(), DAYS[1])
        self.assertEqual(summary["pending_count"], 0)
        self.assertEqual(runner.executor.daily[0]["holdings_value"], D("410.0"))
        self.assertEqual(runner.executor.daily[0]["stale_marks"], "159338.SZ")

    def test_partial_buys_unwind_next_day_and_never_use_yesterdays_pcf(self):
        settings = replace(self.config.simulation, quantity_units=2, max_shares_per_minute=100)
        # Buy half the basket on T, then no later stock bars on T.
        rows = [r for r in self.market_rows() if not (
            r[1] == "000001.SZ" and r[0].date() == DAYS[0] and r[0].time() >= time(9, 32))]
        runner, summary = self.run_model(rows, replace(self.config, quality_check=False, simulation=settings))
        trade = runner.executor.trades[0]
        self.assertEqual(trade.state, "CLOSED")
        self.assertIsNone(trade.applied_at)
        self.assertEqual(trade.reason, "basket_incomplete_at_close")
        self.assertEqual(D(summary["final_net_pnl"]), D("0"))
        self.assertEqual([f["purpose"] for f in runner.executor.fills], ["ENTRY", "UNWIND"])
        self.assertEqual(runner.executor.fills[1]["ts"].date(), DAYS[1])

    def test_provided_suspension_blocks_sale_until_rule_allows_next_day(self):
        self.rules.add(SecurityTradingRule("159338.SZ", DAYS[0], DAYS[0], True, False))
        self.rules.add(SecurityTradingRule("159338.SZ", DAYS[1], DAYS[-1], True, True))
        runner, _ = self.run_model()
        self.assertEqual(next(f for f in runner.executor.fills if f["purpose"] == "EXIT")["ts"].date(), DAYS[1])

    def test_partial_fills_complete_both_legs_and_reconcile(self):
        settings = replace(self.config.simulation, quantity_units=2, max_shares_per_minute=100)
        runner, summary = self.run_model(config=replace(self.config, simulation=settings))
        self.assertEqual(D(summary["final_net_pnl"]), D("96"))
        self.assertEqual([f["quantity"] for f in runner.executor.fills], [100] * 6)
        self.assertTrue(all(o.filled == o.quantity for o in runner.executor.orders))
        self.assertEqual(runner.executor.trades[0].applied_at.time(), time(9, 32))

    def test_future_entry_price_jump_does_not_borrow_reserved_cash(self):
        rows = [(ts, code, 1.1 if code == "000001.SZ" and ts.time() > time(9, 30) else price)
                for ts, code, price in self.market_rows()]
        runner, summary = self.run_model(rows, replace(self.config, starting_cash=D("383")))
        self.assertFalse(runner.executor.fills)
        self.assertEqual(D(summary["final_net_pnl"]), D("0"))

    def test_missing_manager_bars_stay_pending_with_flagged_estimates(self):
        rows = [row for row in self.market_rows() if not (
            row[1] == "600000.SH" and (row[0].date() > DAYS[0] or row[0].time() > time(9, 31)))]
        runner, summary = self.run_model(rows, replace(self.config, quality_check=False))
        self.assertEqual(summary["pending_count"], 1)
        self.assertIsNone(summary["final_net_pnl"])
        self.assertEqual(runner.executor.daily[-1]["stale_cash_substitution"], "600000.SH")
        self.assertFalse(any(f["purpose"] == "MANAGER" for f in runner.executor.fills))

    def test_strict_policy_does_not_invent_security_rules(self):
        config = replace(self.config, simulation=replace(self.config.simulation, security_rule_policy="require"))
        runner, summary = self.run_model(config=config)
        self.assertFalse(runner.executor.fills)
        self.assertEqual(D(summary["final_net_pnl"]), D("0"))
        self.assertEqual(runner.executor.orders[0].last_block, "missing_security_rule")

    def test_account_rejects_short_and_unfunded_fill_atomically(self):
        account = CashAccount(D("100"), PortfolioLedger())
        ts = datetime(2025, 1, 2, 9, 30)
        for side, qty in (("SELL", 1), ("BUY", 101)):
            with self.assertRaises(ValueError):
                account.fill(ts, "T", "000001.SZ", side, qty, D("1"))
        self.assertEqual(account.cash, D("100"))
        self.assertEqual(account.rows, [])

    def test_batch_cache_uses_asof_and_rejects_conflicting_duplicates(self):
        write_market(self.market, self.market_rows())
        cache = DailyCloseCache(self.market)
        cache.load(DAYS[0], {"159338.SZ"})
        self.assertEqual(cache.get("159338.SZ", datetime(2025, 1, 2, 9, 30)).close, D("4"))
        self.assertIsNone(cache.get("159338.SZ", datetime(2025, 1, 2, 9, 36), exact=True))
        write_market(self.market, [(datetime(2025, 1, 2, 9, 30), "159338.SZ", 4.2)], "conflict.parquet")
        with self.assertRaises(MinuteCloseConflictError):
            cache.load(DAYS[0], {"159338.SZ"})

    def test_plan_counts_aggregate_once_and_supports_mandatory_zero_shares(self):
        pcf = FilePCFRepository(self.pcf).get("159338", DAYS[0])
        plan = build_plan(pcf, Side.CREATE, 1)
        self.assertEqual(plan.physical, {"000001.SZ": 100})
        self.assertEqual(plan.manager, {"600000.SH": 100})
        self.assertEqual(plan.manager_fixed, D("30"))
        self.assertEqual(plan.domestic_fixed, D("20"))
        self.assertEqual(plan.substitution_deposit, D("253"))
        with self.assertRaises(ValueError):
            build_plan(replace(pcf, raw_fields=(("PhysicalCreationRedemption", "Y"),)), Side.CREATE, 1)

    def test_config_roundtrip_and_unsupported_model_rejected(self):
        path = self.root / "config.json"
        path.write_text(json.dumps(self.config.to_dict()), encoding="utf-8")
        self.assertEqual(RunnerConfig.from_json(path), self.config)
        with self.assertRaises(ValueError):
            replace(self.config, etf_codes=("512880",))


if __name__ == "__main__":
    unittest.main()
