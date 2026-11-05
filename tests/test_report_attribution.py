import csv
import hashlib
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal as D
from contextlib import redirect_stdout
from io import StringIO
import json
import sys
import unittest
from unittest.mock import patch

import test_full_flow as fixtures
from test_report_totals import FooterParser
from etf_arb.cli import main
from etf_arb.costs import FeeRates
from etf_arb.report.attribution import SIGNAL_BASIS_FIELDS, build_trade_attribution
from etf_arb.report.rebuild import read_rows, rebuild_run_report
from etf_arb.rules import SecurityTradingRule


class ReportAttributionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = replace(self.fixture.config,
            simulation=replace(self.fixture.config.simulation, market_split_execution=True,
                               max_trades_per_day=1, fees=FeeRates()))

    def run_model(self, rows=None, config=None):
        config = config or self.config
        runner, summary = self.fixture.run_model(rows, config)
        run = config.output_dir
        records = build_trade_attribution(read_rows(run / 'trades.csv'), read_rows(run / 'signals.csv'),
            read_rows(run / 'fills.csv'), read_rows(run / 'cash_settlements.csv'),
            read_rows(run / 'fees.csv'), read_rows(run / 'substitution_pricing.csv'))
        # Financial checks below continue to inspect the full internal attribution.
        records = [{key: '' if value is None else str(value) for key, value in r.items()} for r in records]
        return runner, summary, records

    def assert_reconciled(self, row):
        components = ('signal_gross_edge', 'etf_price_effect', 'physical_price_effect',
                      'cash_substitution_pnl', 'estimated_cash_pnl')
        self.assertEqual(sum((D(row[k]) for k in components), D(0)) - D(row['investor_fees']),
                         D(row['net_pnl']))
        self.assertEqual(D(row['attribution_residual']), D(0))
        self.assertEqual(row['attribution_status'], '已核对')

    def test_complete_runner_records_both_directions_and_fees_once(self):
        for side, initial, later in (('CREATE', 4, 4.1), ('REDEEM', 3.2, 3.1)):
            with self.subTest(side=side):
                config = replace(self.config, output_dir=self.fixture.root / side)
                _, summary, rows = self.run_model(self.fixture.market_rows(initial, later), config)
                row = rows[0]
                self.assert_reconciled(row)
                self.assertEqual(row['side'], side)
                self.assertEqual(D(row['signal_spread_per_share']), D(str(initial)) - D('3.6'))
                self.assertEqual(D(row['signal_premium_rate']), (D(str(initial)) - D('3.6')) / D('3.6'))
                self.assertEqual(D(row['signal_gross_edge']), abs(D(str(initial)) - D('3.6')) * 100)
                self.assertEqual(D(row['cash_substitution_pnl']), -D(row['manager_fees']))
                self.assertGreater(D(row['manager_fees']), 0)
                self.assertEqual(D(row['estimated_cash_pnl']), D('-2') if side == 'CREATE' else D('2'))
                self.assertEqual(D(row['net_pnl']), D(summary['final_net_pnl']))
                self.assertNotEqual(D(row['cash_substitution_cash_amount']), D(row['cash_substitution_pnl']))
                report = (config.output_dir / 'report/report.html').read_text(encoding='utf-8')
                for label in ('最终净盈亏', '信号折溢价率', '现金替代价格盈亏', '预估现金盈亏影响'):
                    self.assertIn(label, report)
                self.assertEqual(summary['creation_count'], int(side == 'CREATE'))
                self.assertEqual(summary['redemption_count'], int(side == 'REDEEM'))
                self.assertIn('申购 1 笔，赎回 0 笔' if side == 'CREATE' else '申购 0 笔，赎回 1 笔', report)
                self.assertEqual(FooterParser(report).direction_pnl, {
                    'creation-net-pnl': f'{D(row["net_pnl"]):,.2f}' if side == 'CREATE' else '0.00',
                    'redemption-net-pnl': f'{D(row["net_pnl"]):,.2f}' if side == 'REDEEM' else '0.00',
                })
                self.assertNotIn('已结清套利六列账本（按发起时间）', report)
                self.assertNotIn('max-height:650px', report)
                self.assertNotIn('overflow:auto', report)
                self.assertIn('<tfoot>', report)
                for label in ('信号ETF价', '信号IOPV', '每份价差', '现金替代定价时间',
                              '现金替代到账时间', '实际现金披露日', '现金差额到账时间',
                              '现金替代状态', '现金差额状态', '对账状态'):
                    self.assertNotIn(f'<th>{label}</th>', report)
                self.assertNotIn('<th>状态</th>', report)
                self.assertIn('ledger.html#trade-T000001', report)
                ledger = (config.output_dir / 'report/ledger.html').read_text(encoding='utf-8')
                self.assertIn('id="trade-T000001"', ledger)
                self.assertIn('openLinkedTrade', ledger)
                self.assertIn('信号价差、最终盈亏及两类现金影响', ledger)
                exported = read_rows(config.output_dir / 'report/trade_attribution.csv')
                self.assertEqual(exported, read_rows(config.output_dir / 'trade_attribution.csv'))
                self.assertEqual(D(exported[0]['net_pnl']), D(summary['final_net_pnl']))
                for field in ('state', 'signal_etf_price', 'signal_iopv', 'signal_spread_per_share',
                              'settled_at', 'cash_substitution_status', 'cash_difference_status',
                              'cash_substitution_priced_at', 'cash_substitution_paid_at',
                              'cash_difference_known_on', 'cash_difference_paid_at', 'attribution_status'):
                    self.assertNotIn(field, exported[0])
                self.assertEqual(len(read_rows(config.output_dir / 'portfolio_ledger.csv')[0]), 6)

    def test_negative_estimate_and_actual_scaled_once_for_two_units(self):
        for path in (self.fixture.pcf / '159338').glob('*.xml'):
            path.write_text(path.read_text(encoding='utf-8').replace(
                '<EstimateCashComponent>10</EstimateCashComponent>',
                '<EstimateCashComponent>-10</EstimateCashComponent>').replace(
                '<CashComponent>12</CashComponent>', '<CashComponent>-12</CashComponent>'), encoding='utf-8')
        for side, initial, later in (('CREATE', 4, 4.1), ('REDEEM', 3.2, 3.1)):
            config = replace(self.config, output_dir=self.fixture.root / side,
                             simulation=replace(self.config.simulation, quantity_units=2))
            _, _, rows = self.run_model(self.fixture.market_rows(initial, later), config)
            row = rows[0]
            self.assertEqual(D(row['estimated_cash']), D('-20'))
            self.assertEqual(D(row['actual_cash']), D('-24'))
            self.assertEqual(D(row['estimated_cash_pnl']), D('4') if side == 'CREATE' else D('-4'))
            self.assertEqual(int(row['etf_shares']), 200)
            self.assert_reconciled(row)

    def test_optional_sz_substitution_and_partial_deadline_valuation(self):
        self.fixture.rules.add(SecurityTradingRule('000001.SZ', fixtures.DAYS[0], fixtures.DAYS[-1],
                                                   True, True, trading_start=time(9, 31)))
        scheduled = datetime.combine(fixtures.DAYS[2], time(14, 30))
        deadline = datetime.combine(fixtures.DAYS[2], time(15))
        rows = [(ts, symbol, 1.5 if symbol == '000001.SZ' and ts == scheduled else
                 2 if symbol == '000001.SZ' and ts == deadline else price)
                for ts, symbol, price in self.fixture.market_rows()]
        config = replace(self.config, simulation=replace(self.config.simulation, max_shares_per_minute=40))
        _, _, output = self.run_model(rows, config)
        row = output[0]
        self.assertEqual(int(row['cash_substitution_valued_quantity']), 60)
        self.assertEqual(D(row['cash_substitution_deposit']), D('363'))
        self.assertEqual(D(row['cash_substitution_signal_basis']), D('350'))
        self.assertEqual(D(row['cash_substitution_actual_value']), D('430.06'))
        self.assertEqual(D(row['cash_substitution_pnl']), D('-80.06'))
        self.assertEqual(D(row['cash_substitution_cash_amount']), D('-47.06'))
        self.assertEqual(D(row['physical_price_effect']), D(0))
        self.assert_reconciled(row)

    def test_fixed_redemption_amount_difference_is_in_substitution_pnl(self):
        for path in (self.fixture.pcf / '159338').glob('*.xml'):
            path.write_text(path.read_text(encoding='utf-8').replace(
                '<RedemptionCashSubstitute>20</RedemptionCashSubstitute>',
                '<RedemptionCashSubstitute>15</RedemptionCashSubstitute>').replace(
                '<RedemptionCashSubstitute>30</RedemptionCashSubstitute>',
                '<RedemptionCashSubstitute>25</RedemptionCashSubstitute>'), encoding='utf-8')
        _, _, rows = self.run_model(self.fixture.market_rows(3.2, 3.1))
        self.assertEqual(D(rows[0]['cash_substitution_pnl']), -10-D(rows[0]['manager_fees']))
        self.assert_reconciled(rows[0])

    def test_priced_unpaid_is_pending_and_never_has_final_pnl(self):
        config = replace(self.config, simulation=replace(self.config.simulation,
                             cash_substitution_lag=None, cash_difference_lag=None))
        _, summary, rows = self.run_model(config=config)
        row = rows[0]
        self.assertIsNone(summary['final_net_pnl'])
        self.assertEqual(row['net_pnl'], '')
        self.assertEqual(row['cash_substitution_status'], '已定价待交收')
        self.assertEqual(row['cash_difference_status'], '已定价待交收')
        self.assertEqual(row['cash_substitution_paid_at'], '')
        self.assertNotEqual(row['cash_substitution_pnl'], '')
        self.assertEqual(row['attribution_status'], '待结清')
        report = (config.output_dir / 'report/report.html').read_text(encoding='utf-8')
        self.assertEqual(FooterParser(report).direction_pnl, {
            'creation-net-pnl': '未确定（1 笔）', 'redemption-net-pnl': '0.00',
        })

    def test_missing_deadline_quote_keeps_unpriced_amount_blank(self):
        rows = [r for r in self.fixture.market_rows() if not (
            r[1] == '600000.SH' and r[0] > datetime.combine(fixtures.DAYS[0], time(9, 31)))]
        config = replace(self.config, quality_check=False,
                         simulation=replace(self.config.simulation, market_split_execution=False))
        _, _, records = self.run_model(rows, config)
        self.assertEqual(records[0]['cash_substitution_status'], '未定价')
        self.assertEqual(records[0]['cash_substitution_pnl'], '')
        self.assertEqual(records[0]['net_pnl'], '')

    def test_existing_run_cli_rebuild_restores_legacy_signal_basis_without_changing_run(self):
        self.run_model()
        run = self.config.output_dir
        expected = read_rows(run / 'trade_attribution.csv')
        path = run / 'trades.csv'
        trades = read_rows(path)
        columns = [k for k in trades[0] if k not in SIGNAL_BASIS_FIELDS and k != 'signal_gross_edge']
        with path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, columns, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(trades)
        hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in run.rglob('*') if p.is_file()}
        output = run / 'report_detailed'
        args = ['etf-arb', 'report-attribution', '--run-dir', str(run), '--output', str(output)]
        with patch.object(sys, 'argv', args), redirect_stdout(StringIO()):
            self.assertEqual(main(), 0)
        self.assertEqual(read_rows(output / 'trade_attribution.csv'), expected)
        self.assertEqual(read_rows(output / 'metrics.csv'), read_rows(run / 'report/metrics.csv'))
        self.assertEqual(FooterParser((output / 'report.html').read_text(encoding='utf-8')).direction_pnl,
                         FooterParser((run / 'report/report.html').read_text(encoding='utf-8')).direction_pnl)
        self.assertEqual(json.loads((output / 'report_source.json').read_text(encoding='utf-8'))['trade_count'], 1)
        self.assertEqual(hashes, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in hashes})
        with self.assertRaisesRegex(ValueError, 'not empty'):
            rebuild_run_report(run, output)

    def test_corrupt_closed_attribution_fails_instead_of_showing_misleading_profit(self):
        self.run_model()
        run = self.config.output_dir
        trades = read_rows(run / 'trades.csv')
        trades[0]['net_pnl'] = '99999'
        with self.assertRaisesRegex(ValueError, 'does not reconcile'):
            build_trade_attribution(trades, read_rows(run / 'signals.csv'), read_rows(run / 'fills.csv'),
                                    read_rows(run / 'cash_settlements.csv'), read_rows(run / 'fees.csv'))

    def test_empty_run_has_report_and_header_only_attribution_csv(self):
        _, _, rows = self.run_model(self.fixture.market_rows(3.6, 3.6))
        self.assertEqual(rows, [])
        report = (self.config.output_dir / 'report/report.html').read_text(encoding='utf-8')
        self.assertIn('本轮没有已发起交易', report)


if __name__ == '__main__':
    unittest.main()
