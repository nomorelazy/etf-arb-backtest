import csv
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal as D
from io import StringIO
import json
import sys
import unittest
from unittest.mock import patch

import test_full_flow as fixtures
from etf_arb.cli import main
from etf_arb.costs import FeeRates
from etf_arb.domain import Side, TradeDecision
from etf_arb.full_flow import make_full_runner
from etf_arb.runner_config import RunnerConfig
from etf_arb.simulation_config import SimulationConfig
from test_missing_data import write_market


class TradeDirectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = replace(self.fixture.config, simulation=replace(
            self.fixture.config.simulation, market_split_execution=True,
            entry_at_signal_time=True, premium_threshold=D('.001'),
            max_trades_per_day=1, fees=FeeRates()))

    def test_direction_validation_and_old_config_default(self):
        self.assertTrue(SimulationConfig().allows_side(Side.CREATE))
        self.assertTrue(SimulationConfig().allows_side(Side.REDEEM))
        for mode, allowed, blocked in [('creation', Side.CREATE, Side.REDEEM),
                                       ('redemption', Side.REDEEM, Side.CREATE)]:
            settings = SimulationConfig(trade_direction=mode)
            self.assertTrue(settings.allows_side(allowed))
            self.assertFalse(settings.allows_side(blocked))
        for invalid in (None, True, [], 'REDEEM', ''):
            with self.assertRaises(ValueError):
                SimulationConfig(trade_direction=invalid)
        raw = self.config.to_dict()
        del raw['simulation']['trade_direction']
        path = self.fixture.root / 'old.json'
        path.write_text(json.dumps(raw), encoding='utf-8')
        self.assertEqual(RunnerConfig.from_json(path).simulation.trade_direction, 'both')

    def test_cli_only_redeems_after_strict_boundary_without_creation_cooldown(self):
        rules = self.fixture.root / 'rules.json'
        rules.write_text(json.dumps({'etf_rules': [{
            'etf_code': '159338', 'effective_from': '2025-01-02', 'effective_to': '2025-01-06',
            'creation_enabled': True, 'redemption_enabled': True,
            'creation_cutoff': '14:55:00', 'redemption_cutoff': '14:55:00'}],
            'security_rules': []}), encoding='utf-8')
        config = replace(self.config, rules_path=rules)
        path = self.fixture.root / 'cli.json'
        path.write_text(json.dumps(config.to_dict()), encoding='utf-8')
        rows = [(ts, symbol, (4 if ts.time() == time(9, 30) else
                             3.5964 if ts.time() == time(9, 31) else 3.59)
                 if symbol == '159338.SZ' else price)
                for ts, symbol, price in self.fixture.market_rows()]
        write_market(self.fixture.market, rows)
        args = ['etf-arb', 'backtest', '--config', str(path),
                '--direction', 'redemption', '--premium-threshold', '0.001']
        with patch.object(sys, 'argv', args), redirect_stdout(StringIO()) as output:
            self.assertEqual(main(), 0, output.getvalue())

        def read(name):
            with (config.output_dir / name).open(encoding='utf-8-sig', newline='') as handle:
                return list(csv.DictReader(handle))

        trades = read('trades.csv')
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]['side'], 'REDEEM')
        self.assertEqual(trades[0]['opened_at'], '2025-01-02 09:32:00')
        self.assertEqual(trades[0]['applied_at'], '2025-01-02 09:33:00')
        signals = {r['ts']: r for r in read('signals.csv')}
        self.assertEqual(signals['2025-01-02 09:30:00']['reason'], 'direction_disabled')
        self.assertEqual(signals['2025-01-02 09:31:00']['reason'], 'below_threshold')
        fills = read('fills.csv')
        entry = next(r for r in fills if r['purpose'] == 'ENTRY')
        self.assertEqual((entry['symbol'], entry['ts'], D(entry['price'])),
                         ('159338.SZ', '2025-01-02 09:32:00', D('3.59')))
        self.assertTrue(all(r['ts'] == '2025-01-02 09:33:00' for r in fills if r['purpose'] != 'ENTRY'))
        summary = json.loads((config.output_dir / 'financial_summary.json').read_text(encoding='utf-8'))
        self.assertEqual((summary['trade_direction'], summary['creation_count'], summary['redemption_count']),
                         ('redemption', 0, 1))
        self.assertEqual(summary['pending_count'], 0)
        self.assertEqual(D(read('trade_attribution.csv')[0]['etf_price_effect']), 0)
        self.assertEqual(D(trades[0]['net_pnl']), D(summary['ending_cash'])-config.starting_cash)
        self.assertGreater(D(summary['fee']), 0)
        self.assertEqual(len(read('portfolio_ledger.csv')[0]), 6)
        saved = json.loads((config.output_dir / 'config.json').read_text(encoding='utf-8'))
        assumptions = json.loads((config.output_dir / 'assumptions.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['simulation']['trade_direction'], 'redemption')
        self.assertEqual(assumptions['trade_direction'], 'redemption')
        html = (config.output_dir / 'report/report.html').read_text(encoding='utf-8')
        self.assertIn('本轮仅做赎回：折价幅度严格大于 0.1000%', html)
        self.assertIn('申购 0 笔，赎回 1 笔', html)

    def test_executor_rejects_external_creation_before_cash_or_orders(self):
        config = replace(self.config, simulation=replace(self.config.simulation, trade_direction='redemption'))
        write_market(self.fixture.market, self.fixture.market_rows())
        runner = make_full_runner(config, rules=self.fixture.rules)
        runner.source = None
        decision = TradeDecision('159338', datetime.combine(fixtures.DAYS[0], time(9, 30)),
                                 True, Side.CREATE, 1, D(40), D(0), D(40), ('external',))
        runner.run([decision])
        self.assertFalse(runner.executor.trades)
        self.assertFalse(runner.executor.orders)
        self.assertFalse(runner.executor.account.rows)
        self.assertEqual(runner.executor.account.cash, config.starting_cash)
        self.assertEqual(runner.executor.rejections[0]['reason'], 'direction_disabled')

    def test_asymmetric_cli_thresholds_use_strict_boundaries_and_report_both(self):
        rules = self.fixture.root / 'rules.json'
        rules.write_text(json.dumps({'etf_rules': [{
            'etf_code': '159338', 'effective_from': '2025-01-02', 'effective_to': '2025-01-06',
            'creation_enabled': True, 'redemption_enabled': True,
            'creation_cutoff': '14:55:00', 'redemption_cutoff': '14:55:00'}],
            'security_rules': []}), encoding='utf-8')
        for side, boundary, crossing in [('CREATE', 3.6072, 3.6073), ('REDEEM', 3.5946, 3.5945)]:
            with self.subTest(side=side):
                target = self.fixture.root / side
                config = replace(self.config, rules_path=rules, output_dir=target)
                path = self.fixture.root / f'{side}.json'
                path.write_text(json.dumps(config.to_dict()), encoding='utf-8')
                prices = {time(9, 30): boundary, time(9, 31): crossing}
                rows = [(ts, symbol, prices.get(ts.time(), price) if symbol == '159338.SZ' else price)
                        for ts, symbol, price in self.fixture.market_rows()]
                write_market(self.fixture.market, rows)
                args = ['etf-arb', 'backtest', '--config', str(path),
                        '--creation-threshold', '0.002', '--redemption-threshold', '0.0015']
                with patch.object(sys, 'argv', args), redirect_stdout(StringIO()) as output:
                    self.assertEqual(main(), 0, output.getvalue())
                with (target / 'signals.csv').open(encoding='utf-8-sig', newline='') as handle:
                    signals = {row['ts']: row for row in csv.DictReader(handle)}
                self.assertEqual(signals['2025-01-02 09:30:00']['reason'], 'below_threshold')
                self.assertEqual(signals['2025-01-02 09:31:00']['side'], side)
                with (target / 'trades.csv').open(encoding='utf-8-sig', newline='') as handle:
                    trade, = csv.DictReader(handle)
                self.assertEqual((trade['side'], trade['opened_at']), (side, '2025-01-02 09:31:00'))
                summary = json.loads((target / 'financial_summary.json').read_text(encoding='utf-8'))
                self.assertEqual((D(summary['creation_threshold']), D(summary['redemption_threshold'])),
                                 (D('.002'), D('.0015')))
                report = (target / 'report/report.html').read_text(encoding='utf-8')
                self.assertIn('申购溢价严格大于 0.2000%', report)
                self.assertIn('赎回折价幅度严格大于 0.1500%', report)


if __name__ == '__main__':
    unittest.main()
