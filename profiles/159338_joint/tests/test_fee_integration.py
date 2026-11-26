import csv
import json
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal as D
import unittest

import test_full_flow as fixtures
from etf_arb.costs import AssetType, FeeBreakdown, FeeRates, TradeFeeModel
from etf_arb.full_flow import make_full_runner
from etf_arb.portfolio.account import CashAccount
from etf_arb.portfolio.ledger import PortfolioLedger
from etf_arb.runner_config import RunnerConfig
from etf_arb.simulation_config import SimulationConfig
from test_missing_data import write_market


class FeeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = replace(self.fixture.config,
            simulation=replace(self.fixture.config.simulation, fees=FeeRates()))

    def run_model(self, rows=None, config=None):
        return self.fixture.run_model(rows, config or self.config)

    def test_create_net_cashflows_and_manager_refund_include_fees_once(self):
        runner, summary = self.run_model()
        self.assertEqual(D(summary['final_net_pnl']), D('47.90'))
        self.assertEqual(D(summary['fee']), D('0.10'))
        self.assertEqual(D(summary['investor_fees']), D('0.06'))
        self.assertEqual(D(summary['manager_fees']), D('0.04'))
        self.assertEqual(runner.executor.trades[0].legs[1].final, D('22.96'))
        self.assertEqual(D(summary['ending_cash']), D('10047.90'))
        own_events = [row for row in runner.executor.account.rows if row['event'].endswith('_PAID')]
        self.assertEqual(-sum(row['cash_delta'] for row in own_events), D('0.06'))
        manager = next(row for row in runner.executor.fills if row['purpose'] == 'MANAGER')
        self.assertEqual(manager['fee'], D('0.04'))
        self.assertEqual(manager['commission'], D('0.02'))
        self.assertFalse(any(row['event'].endswith('_PAID') and row['symbol'] == '600000.SH'
                             for row in runner.executor.account.rows))
        rows = runner.resources.portfolio.to_rows()
        self.assertEqual(rows[0]['交易额度（元）'], '410.0')
        self.assertEqual(D(rows[0]['交易盈亏（元）']), D('47.90'))
        self.assertEqual(len(rows[0]), 6)

    def test_redeem_net_manager_income_has_sell_tax_and_no_etf_stamp_tax(self):
        runner, summary = self.run_model(self.fixture.market_rows(3.2, 3.1))
        self.assertEqual(D(summary['final_net_pnl']), D('51.76'))
        self.assertEqual(D(summary['fee']), D('0.24'))
        self.assertEqual(D(summary['investor_fees']), D('0.10'))
        self.assertEqual(D(summary['manager_fees']), D('0.14'))
        self.assertEqual(runner.executor.trades[0].legs[1].final, D('249.86'))
        stamp = sum(row['stamp_duty'] for row in runner.executor.fee_rows)
        self.assertEqual(stamp, D('0.15'))
        self.assertTrue(all(row['stamp_duty'] == 0 and row['transfer_fee'] == 0
                            for row in runner.executor.fee_rows if row['symbol'] == '159338.SZ'))

    def test_unfilled_manager_close_valuation_does_not_invent_execution_fees(self):
        at = datetime.combine(fixtures.DAYS[-1], time(14,30))
        rows = [row for row in self.fixture.market_rows() if not (row[1] == '600000.SH' and row[0] == at)]
        runner, summary = self.run_model(rows, replace(self.config, quality_check=False))
        self.assertEqual(D(summary['final_net_pnl']), D('47.94'))
        self.assertEqual(D(summary['manager_fees']), D('0'))
        self.assertFalse(any(row['purpose'] == 'MANAGER' for row in runner.executor.fee_rows))
        self.assertEqual(runner.executor.substitution_pricing[0]['quantity'], 100)

    def test_partial_manager_fills_charge_only_filled_quantity(self):
        settings = replace(self.config.simulation, quantity_units=2, max_shares_per_minute=100)
        runner, summary = self.run_model(config=replace(self.config, simulation=settings))
        self.assertEqual(D(summary['final_net_pnl']), D('95.85'))
        self.assertEqual(D(summary['fee']), D('0.15'))
        self.assertEqual(D(summary['manager_fees']), D('0.04'))
        self.assertEqual(runner.executor.substitution_pricing[0]['quantity'], 100)

    def test_entry_funding_includes_fees_before_opening_a_trade(self):
        runner, summary = self.run_model(config=replace(self.config, starting_cash=D('383.01')))
        self.assertEqual(summary['opened_count'], 0)
        self.assertEqual(runner.executor.account.rows, [])
        self.assertTrue(all(row['reason'] == 'insufficient_cash' for row in runner.executor.rejections))

    def test_fee_cash_movements_are_atomic_when_total_funding_is_insufficient(self):
        account = CashAccount(D('1000199.99'), PortfolioLedger())
        fee = TradeFeeModel().calculate(asset_type=AssetType.STOCK, side='BUY', turnover=D('1000000'))
        with self.assertRaisesRegex(ValueError, 'including transaction fees'):
            account.fill(datetime(2025,1,2,9,35), 'T', '000001.SZ', 'BUY', 100000, D('10'), fees=fee)
        self.assertEqual(account.rows, [])
        self.assertEqual(account.cash, D('1000199.99'))
        self.assertEqual(account.holdings('T'), {})

    def test_full_basket_preflight_reserves_fees_at_the_execution_price(self):
        rows = [(ts, code, 1.2 if code == '000001.SZ' and ts.time() > time(9,30) else price)
                for ts, code, price in self.fixture.market_rows()]
        write_market(self.fixture.market, rows)
        runner = make_full_runner(replace(self.config, starting_cash=D('403.01')), rules=self.fixture.rules)
        with self.assertRaisesRegex(ValueError, 'Full-basket assumption cannot be funded'):
            runner.run()
        self.assertEqual(runner.executor.account.rows, [])
        self.assertEqual(runner.executor.fills, [])
        self.assertEqual(runner.executor.account.cash, D('403.01'))

    def test_partial_own_orders_reserve_fees_in_affordable_quantity(self):
        config = replace(self.config, starting_cash=D('483.03'), simulation=replace(
            self.config.simulation, quantity_units=1, assume_full_basket_fill=False))
        rows = [(ts, code, 2 if code == '000001.SZ' and ts.time() > time(9,30) else price)
                for ts, code, price in self.fixture.market_rows()]
        runner, _ = self.run_model(rows, config)
        self.assertEqual(runner.executor.fills, [])
        self.assertEqual(runner.executor.account.cash, D('483.03'))

    def test_signal_estimates_fees_without_changing_the_raw_threshold(self):
        settings = replace(self.config.simulation, fees=replace(FeeRates(), stock_commission=D('0.2')))
        runner, _ = self.run_model(config=replace(self.config, simulation=settings))
        row = next(row for row in runner.source.rows if row['reason'] == 'threshold_crossed')
        self.assertGreater(row['estimated_fee'], 0)
        self.assertLess(row['estimated_net_edge'], 0)
        self.assertEqual(len(runner.executor.trades), 1)

    def test_fee_config_roundtrip_defaults_and_zero_override(self):
        self.assertEqual(SimulationConfig().fees, FeeRates())
        path = self.fixture.root/'config.json'
        path.write_text(json.dumps(self.config.to_dict()), encoding='utf-8')
        self.assertEqual(RunnerConfig.from_json(path), self.config)
        self.assertEqual(SimulationConfig(fees={'stock_commission':'0.0001'}).fees.stock_commission, D('0.0001'))
        with self.assertRaises(ValueError):
            SimulationConfig(fees={'unknown':'0.1'})
        with self.assertRaises(ValueError):
            FeeBreakdown(commission=D('-1'))
        runner, summary = self.run_model(config=replace(self.config,
            simulation=replace(self.config.simulation, fees=FeeRates.zero())))
        self.assertEqual(D(summary['fee']), 0)
        self.assertEqual(D(summary['final_net_pnl']), D('48'))

    def test_standard_reports_show_fee_breakdown_and_metrics_total(self):
        _, summary = self.run_model()
        out = self.config.output_dir
        with (out/'fees.csv').open(encoding='utf-8-sig', newline='') as handle:
            fees = list(csv.DictReader(handle))
        self.assertEqual(sum(D(row['total']) for row in fees), D(summary['fee']))
        self.assertEqual({row['charged_to'] for row in fees}, {'investor_cash','cash_substitution'})
        with (out/'report/metrics.csv').open(encoding='utf-8-sig', newline='') as handle:
            metrics = {row['metric']: row['value'] for row in csv.DictReader(handle)}
        self.assertEqual(D(metrics['total_costs']), D('0.10'))
        self.assertIn('交易税费', (out/'report/report.html').read_text(encoding='utf-8'))
        self.assertIn('逐笔交易税费', (out/'report/ledger.html').read_text(encoding='utf-8'))
        self.assertNotIn('手续费及额外滑点为零', (out/'report/ledger.html').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
