import csv
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal as D
import json
from contextlib import redirect_stdout
from io import StringIO
import sys
import unittest
from unittest.mock import patch

import test_full_flow as fixtures
from etf_arb.costs import FeeRates
from etf_arb.domain import Side, TradeDecision
from etf_arb.full_flow import make_full_runner
from etf_arb.rules import ETFRule, InMemoryRuleRepository, SecurityTradingRule
from etf_arb.runner_config import RunnerConfig
from etf_arb.simulation_config import SimulationConfig
from test_missing_data import write_market


class SignalTimeEntryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.signal = datetime.combine(fixtures.DAYS[0], time(9, 30))
        self.application = self.signal + timedelta(minutes=1)
        self.settings = replace(self.fixture.config.simulation, market_split_execution=True,
                                entry_at_signal_time=True, max_trades_per_day=1, fees=FeeRates())
        self.config = replace(self.fixture.config, simulation=self.settings)

    def read(self, name, config=None):
        with ((config or self.config).output_dir / name).open(encoding='utf-8-sig', newline='') as f:
            return list(csv.DictReader(f))

    def rows(self, side=Side.CREATE):
        return [(ts, symbol, (4 if ts == self.signal else 4.2 if ts == self.application else 4.5)
                 if symbol == '159338.SZ' and side is Side.CREATE else
                 (3.2 if ts == self.signal else 9.9) if symbol == '159338.SZ' else
                 (price if ts == self.signal else 1.5 if symbol == '000001.SZ' else 2.5))
                for ts, symbol, price in self.fixture.market_rows()]

    def run_model(self, rows=None, config=None):
        return self.fixture.run_model(self.rows() if rows is None else rows, config or self.config)

    def test_creation_buys_at_signal_but_keeps_application_and_etf_exit_times(self):
        runner, summary = self.run_model()
        entry = next(f for f in runner.executor.fills if f['purpose'] == 'ENTRY')
        exit_fill = next(f for f in runner.executor.fills if f['purpose'] == 'EXIT')
        manager = next(f for f in runner.executor.fills if f['purpose'] == 'MANAGER')
        trade = runner.executor.trades[0]
        self.assertEqual((entry['ts'], entry['price'], entry['basis']),
                         (self.signal, D('1'), 'signal_minute_close'))
        self.assertEqual(trade.applied_at, self.application)
        self.assertEqual((manager['ts'], manager['price']), (self.application, D('2.5')))
        self.assertEqual((exit_fill['ts'], exit_fill['price']),
                         (self.signal + timedelta(minutes=2), D('4.5')))
        row = self.read('trade_attribution.csv')[0]
        self.assertEqual(D(row['physical_price_effect']), D(0))
        self.assertEqual(D(row['etf_price_effect']), D('50'))
        self.assertTrue(summary['entry_at_signal_time'])
        self.assertEqual(runner.executor.account.cash - self.config.starting_cash, D(row['net_pnl']))
        self.assertEqual(len(runner.resources.portfolio.to_rows()[0]), 6)

    def test_redemption_etf_buy_at_signal_and_both_stock_sales_at_t_plus_one(self):
        runner, summary = self.run_model(self.rows(Side.REDEEM))
        entry = next(f for f in runner.executor.fills if f['purpose'] == 'ENTRY')
        sales = [f for f in runner.executor.fills if f['side'] == 'SELL']
        self.assertEqual((entry['ts'], entry['price']), (self.signal, D('3.2')))
        self.assertEqual({f['ts'] for f in sales}, {self.application})
        self.assertEqual({f['symbol']: f['price'] for f in sales},
                         {'000001.SZ': D('1.5'), '600000.SH': D('2.5')})
        self.assertEqual(runner.executor.trades[0].redemption_prices_locked_at, self.application)
        self.assertEqual(summary['redemption_price_locked_count'], 1)
        row = self.read('trade_attribution.csv')[0]
        self.assertEqual(D(row['etf_price_effect']), D(0))
        self.assertEqual(runner.executor.account.cash - self.config.starting_cash, D(row['net_pnl']))
        self.assertEqual(sum(f['purpose'] == 'ENTRY' for f in runner.executor.fills), 1)

    def test_new_entry_is_paid_before_application_and_fees_are_not_repeated(self):
        runner, _ = self.run_model(self.rows(Side.REDEEM))
        flows = runner.executor.account.rows
        application_flow = next(r for r in flows if r['event'] == 'REDEEM_SUBMITTED')
        self.assertEqual(application_flow['ts'], self.application)
        self.assertTrue(any(r['ts'] == self.signal and r['cash_delta'] == D('-320') for r in flows))
        entries = [f for f in runner.executor.fee_rows if f['purpose'] == 'ENTRY']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['ts'], self.signal)
        self.assertEqual(sum((r['total'] for r in entries), D(0)), D('0.03'))

    def test_bought_assets_do_not_require_another_buy_quote_at_application(self):
        for side, symbol in ((Side.CREATE, '000001.SZ'), (Side.REDEEM, '159338.SZ')):
            rows = [r for r in self.rows(side) if (r[0], r[1]) != (self.application, symbol)]
            config = replace(self.config, quality_check=False, output_dir=self.fixture.root / side.value)
            runner, summary = self.run_model(rows, config)
            self.assertEqual(runner.executor.trades[0].applied_at, self.application)
            self.assertEqual(summary['pending_count'], 0)
            self.assertEqual(sum(f['purpose'] == 'ENTRY' for f in runner.executor.fills), 1)

    def test_future_entry_price_jump_does_not_reprice_already_bought_stock(self):
        rows = [(ts, symbol, 100 if symbol == '000001.SZ' and ts > self.signal else price)
                for ts, symbol, price in self.fixture.market_rows()]
        config = replace(self.config, starting_cash=D('383'),
                         simulation=replace(self.settings, fees=FeeRates.zero()))
        runner, summary = self.run_model(rows, config)
        self.assertEqual(next(f['price'] for f in runner.executor.fills if f['purpose'] == 'ENTRY'), D('1'))
        self.assertEqual(runner.executor.trades[0].applied_at, self.application)
        self.assertEqual(summary['pending_count'], 0)

    def test_stale_signal_etf_price_cannot_be_used_as_an_actual_buy(self):
        rows = [r for r in self.fixture.market_rows(3.2, 3.6)
                if (r[0], r[1]) != (self.signal, '159338.SZ')]
        rows.append((self.signal - timedelta(minutes=1), '159338.SZ', 3.2))
        runner, _ = self.run_model(rows, replace(self.config, quality_check=False))
        self.assertFalse(runner.executor.trades)
        self.assertFalse(runner.executor.fills)
        self.assertIn('missing_entry_quote', {r['reason'] for r in runner.executor.rejections})

    def test_entry_permission_and_application_cutoff_are_checked_before_buying(self):
        for case in ('buy_blocked', 'application_cutoff'):
            with self.subTest(case=case):
                if case == 'buy_blocked':
                    rules = InMemoryRuleRepository([
                        ETFRule('159338', fixtures.DAYS[0], fixtures.DAYS[-1], True, True,
                                time(14, 55), time(14, 55))])
                    rules.add(SecurityTradingRule('159338.SZ', fixtures.DAYS[0], fixtures.DAYS[-1],
                                                  True, True, trading_start=time(9, 31)))
                else:
                    rules = InMemoryRuleRepository([
                        ETFRule('159338', fixtures.DAYS[0], fixtures.DAYS[-1], True, True,
                                time(9, 30), time(9, 30))])
                write_market(self.fixture.market, self.rows(Side.REDEEM))
                runner = make_full_runner(replace(self.config, output_dir=self.fixture.root / case), rules=rules)
                with self.assertRaises(ValueError):
                    runner.run()
                self.assertFalse(runner.executor.fills)
                self.assertFalse(runner.executor.account.rows)

    def test_no_application_minute_rejects_external_decision_before_entry(self):
        config = replace(self.config, clock_times=(time(9, 30), time(9, 32), time(15)))
        write_market(self.fixture.market, self.rows())
        runner = make_full_runner(config, rules=self.fixture.rules)
        decision = TradeDecision('159338', self.signal, True, Side.CREATE, 1, D(1), D(0), D(1))
        runner.run([decision])
        self.assertFalse(runner.executor.fills)
        self.assertFalse(runner.executor.account.rows)

    def test_cash_substituted_sz_still_waits_until_t_plus_two_1430(self):
        self.fixture.rules.add(SecurityTradingRule('000001.SZ', fixtures.DAYS[0], fixtures.DAYS[-1],
                                                   True, True, trading_start=time(9, 31)))
        runner, _ = self.run_model()
        replacement = next(f for f in runner.executor.fills if f['symbol'] == '000001.SZ')
        self.assertEqual(replacement['purpose'], 'MANAGER')
        self.assertEqual(replacement['ts'], datetime.combine(fixtures.DAYS[2], time(14, 30)))
        self.assertFalse(any(f['purpose'] == 'ENTRY' for f in runner.executor.fills))
        self.assertEqual(runner.executor.trades[0].applied_at, self.application)

    def test_new_timing_keeps_pending_settlement_and_original_opening_date(self):
        config = replace(self.config, simulation=replace(self.settings,
            cash_substitution_lag=None, cash_difference_lag=None))
        runner, summary = self.run_model(self.rows(Side.REDEEM), config)
        self.assertIsNone(summary['final_net_pnl'])
        self.assertEqual(summary['pending_count'], 1)
        self.assertEqual(self.read('trade_attribution.csv', config)[0]['net_pnl'], '')
        self.assertEqual(runner.resources.portfolio.trades[0].opened_at, self.signal)

    def test_interval_still_starts_at_application_not_the_earlier_buy(self):
        clocks = (time(9, 30), time(9, 31), time(9, 32), time(10, 30), time(10, 31), time(10, 32), time(15))
        rows = [(datetime.combine(day, clock), symbol, price)
                for day in fixtures.DAYS for clock in clocks
                for symbol, price in (('159338.SZ', 4), ('000001.SZ', 1), ('600000.SH', 2))]
        config = replace(self.config, clock_times=clocks,
                         simulation=replace(self.settings, max_trades_per_day=None))
        runner, _ = self.run_model(rows, config)
        self.assertEqual([t.decision.ts.time() for t in runner.executor.trades], [time(9, 30), time(10, 31)])
        self.assertEqual([t.applied_at.time() for t in runner.executor.trades], [time(9, 31), time(10, 32)])

    def test_explicit_false_retains_legacy_next_minute_entry(self):
        config = replace(self.config, simulation=replace(self.settings, entry_at_signal_time=False))
        runner, _ = self.run_model(self.fixture.market_rows(3.2, 3.1), config)
        entry = next(f for f in runner.executor.fills if f['purpose'] == 'ENTRY')
        self.assertEqual((entry['ts'], entry['price'], entry['basis']),
                         (self.application, D('3.1'), 'next_minute_close'))

    def test_signal_buy_requires_cash_including_fees_and_application_reserve(self):
        for side, cash in ((Side.CREATE, D('383.01')), (Side.REDEEM, D('320'))):
            config = replace(self.config, starting_cash=cash, output_dir=self.fixture.root / side.value)
            runner, summary = self.run_model(self.rows(side), config)
            self.assertEqual(summary['opened_count'], 0)
            self.assertFalse(runner.executor.fills)
            self.assertFalse(runner.executor.account.rows)
            self.assertEqual(runner.executor.account.cash, cash)

    def test_cli_uses_signal_entry_by_default_when_old_json_has_no_new_flag(self):
        from etf_arb.cli import main
        rules_path = self.fixture.root / 'rules.json'
        rules_path.write_text(json.dumps({'etf_rules': [{
            'etf_code': '159338', 'effective_from': '2025-01-02', 'effective_to': '2025-01-06',
            'creation_enabled': True, 'redemption_enabled': True,
            'creation_cutoff': '14:55:00', 'redemption_cutoff': '14:55:00'}],
            'security_rules': []}), encoding='utf-8')
        config = replace(self.config, rules_path=rules_path)
        raw = config.to_dict()
        del raw['simulation']['entry_at_signal_time']
        path = self.fixture.root / 'old_settings.json'
        path.write_text(json.dumps(raw), encoding='utf-8')
        write_market(self.fixture.market, self.rows(Side.REDEEM))
        with patch.object(sys, 'argv', ['etf-arb', 'backtest', '--config', str(path)]), redirect_stdout(StringIO()) as output:
            result = main()
        self.assertEqual(result, 0, output.getvalue())
        entry = next(r for r in self.read('fills.csv', config) if r['purpose'] == 'ENTRY')
        self.assertEqual(datetime.fromisoformat(entry['ts']), self.signal)
        self.assertEqual(entry['basis'], 'signal_minute_close')
        saved = json.loads((config.output_dir / 'config.json').read_text(encoding='utf-8'))
        self.assertTrue(saved['simulation']['entry_at_signal_time'])

    def test_default_validation_roundtrip_and_formal_report_describe_new_timing(self):
        self.assertTrue(SimulationConfig().entry_at_signal_time)
        for invalid in (None, 1, 'true'):
            with self.assertRaises(ValueError):
                SimulationConfig(entry_at_signal_time=invalid)
        with self.assertRaisesRegex(ValueError, 'requires assume_full_basket_fill'):
            SimulationConfig(market_split_execution=False, assume_full_basket_fill=False)
        path = self.fixture.root / 'settings.json'
        path.write_text(json.dumps(self.config.to_dict()), encoding='utf-8')
        self.assertEqual(RunnerConfig.from_json(path), self.config)
        self.run_model()
        assumptions = json.loads((self.config.output_dir / 'assumptions.json').read_text(encoding='utf-8'))
        self.assertTrue(assumptions['entry_at_signal_time'])
        report = (self.config.output_dir / 'report/report.html').read_text(encoding='utf-8')
        self.assertIn('信号 t 分钟的精确 close', report)
        self.assertIn('t+1 分钟提交申赎', report)


if __name__ == '__main__':
    unittest.main()
