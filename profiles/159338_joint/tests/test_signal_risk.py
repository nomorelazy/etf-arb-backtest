from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal as D
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from etf_arb.domain import PCF, PCFComponent
from etf_arb.report.signal_risk import (build_signal_risk, load_signal_risk,
    save_signal_risk, signal_risk_html)
from etf_arb.rules import InMemoryRuleRepository, SecurityTradingRule


class SignalRiskTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.day, self.at = date(2025, 1, 2), datetime(2025, 1, 2, 9, 35)
        self.minute = self.root / 'market'
        (self.minute / '2025-01').mkdir(parents=True)
        (self.root / 'pcf').mkdir()
        self.components = (
            PCFComponent('000001', D(100), 'Upper', '1', market_code='102'),
            PCFComponent('600000', D(100), 'Lower', '1', market_code='101'),
            PCFComponent('000002', D(100), 'Overlap', '2', creation_cash_substitute=D(500),
                         redemption_cash_substitute=D(900), market_code='102'),
            PCFComponent('000003', D(0), 'Fixed zero shares', '2', creation_cash_substitute=D(200), market_code='102'),
            PCFComponent('000005', D(100), 'Normal', '0', market_code='102'),
            PCFComponent('000006', D(0), 'Zero nonmandatory', '1', market_code='102'),
            PCFComponent('159900', D(0), 'Cash placeholder', '2', creation_cash_substitute=D(99999),
                         market_code='102', is_cash_placeholder=True),
        )
        self.pcf = PCF('159338', self.day, 1000, self.components, pre_trading_day=date(2024, 12, 31))
        self.bounds = self.root / 'bounds.csv'
        with self.bounds.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['date', 'symbol', 'upper_limit', 'lower_limit'])
            for symbol in ('000001.SZ', '600000.SH', '000002.SZ', '000003.SZ', '000005.SZ'):
                writer.writerow([self.day, symbol, '11', '9'])
        self.config = dict(pcf_root=str(self.root / 'pcf'), minute_root=str(self.minute),
            daily_price_limits_path=str(self.bounds), etf_codes=['159338'])
        self.rules = InMemoryRuleRepository(security_rules=[SecurityTradingRule(
            component.security_code, self.day, self.day, True, True,
            is_suspended=component.security_code == '000002') for component in self.components
            if not component.is_cash_placeholder])
        self.market = {'000001.SZ': 11., '600000.SH': 9., '000002.SZ': 11., '000003.SZ': 10., '000005.SZ': 10.}
        self.write_market()

    def write_market(self, extra=()):
        rows = [(self.at, symbol, price) for symbol, price in self.market.items()] + list(extra)
        pq.write_table(pa.table({'time': [row[0] for row in rows],
            'thscode': [row[1] for row in rows], 'close': [row[2] for row in rows]}),
            self.minute / '2025-01' / 'quotes.parquet')

    def trade(self, tid='T1', side='CREATE', units=2, *, code='159338', market='SZ'):
        return dict(trade_id=tid, opened_at=self.at, side=side, quantity_units=units,
                    etf_shares=1000 * units, etf_symbol=f'{code}.{market}')

    def build(self, trades=None, rules=None, signals=None):
        with patch('etf_arb.report.signal_risk.FilePCFRepository.get', return_value=self.pcf):
            return build_signal_risk(self.config, trades or [self.trade()],
                signals if signals is not None else [dict(ts=self.at, iopv='10')],
                rules=rules if rules is not None else self.rules)

    def test_directional_selection_and_overlapping_categories_are_deduplicated(self):
        data = self.build([self.trade(), self.trade('T2', 'REDEEM')])
        create, redeem = data['trades']
        self.assertEqual(create['limit_value'], D(3200))
        self.assertEqual(create['suspension_value'], D(1000))
        self.assertEqual(create['mandatory_cash_value'], D(1400))
        self.assertEqual(create['total_value'], D(3600))
        self.assertEqual(create['total_weight'], D('.18'))
        self.assertEqual(create['total_stock_count'], 3)
        self.assertEqual(redeem['total_value'], D(1400))
        self.assertEqual(redeem['limit_value'], D(0))
        self.assertEqual(redeem['total_weight'], D('.07'))
        self.assertEqual(create['etf_market'], 'SZ')
        self.assertEqual(create['excluded_cross_market_cash_count'], 1)
        self.assertEqual(create['component_count'], 4)
        create_rows = {row['symbol']: row for row in data['rows'] if row['trade_id'] == 'T1'}
        self.assertEqual(set(create_rows), {'000001.SZ', '000002.SZ', '000003.SZ'})
        self.assertEqual(create_rows['000002.SZ']['risk_tags'], 'limit_up;suspension;mandatory_cash')
        self.assertEqual(create_rows['000002.SZ']['signal_value'], D(1000))
        self.assertEqual(create_rows['000003.SZ']['quantity'], D(0))
        self.assertEqual(create_rows['000003.SZ']['signal_value'], D(400))
        self.assertTrue(create['coverage_complete'])

    def test_fixed_zero_share_stock_survives_without_any_quote(self):
        del self.market['000003.SZ']
        self.write_market()
        data = self.build()
        fixed = next(row for row in data['rows'] if row['symbol'] == '000003.SZ')
        self.assertIsNone(fixed['signal_price'])
        self.assertEqual(fixed['signal_value'], D(400))
        self.assertEqual(data['trades'][0]['total_value'], D(3600))
        self.assertEqual(data['trades'][0]['limit_unknown_stock_count'], 1)

    def test_missing_suspension_records_preserve_known_partial_not_confirmed_zero(self):
        data = self.build(rules=InMemoryRuleRepository())
        summary = data['trades'][0]
        self.assertEqual(summary['suspension_stock_count'], 0)
        self.assertEqual(summary['suspension_unknown_stock_count'], 4)
        self.assertIsNone(summary['suspension_value'])
        self.assertEqual(summary['total_known_value'], D(3600))
        self.assertIsNone(summary['total_value'])
        self.assertFalse(summary['coverage_complete'])
        self.assertIn('已识别', signal_risk_html(data))
        self.assertIn('停牌状态缺失 4 只', signal_risk_html(data))

    def test_no_previous_minute_or_future_fallback_and_missing_quote_is_not_suspension(self):
        del self.market['000001.SZ']
        self.write_market([(self.at - timedelta(minutes=1), '000001.SZ', 11.),
                           (self.at + timedelta(minutes=1), '000001.SZ', 11.)])
        data = self.build()
        self.assertNotIn('000001.SZ', {row['symbol'] for row in data['rows']})
        summary = data['trades'][0]
        self.assertEqual(summary['limit_unknown_stock_count'], 1)
        self.assertEqual(summary['suspension_stock_count'], 1)
        self.assertEqual(summary['suspension_unknown_stock_count'], 0)
        self.assertIsNone(summary['total_value'])
        self.assertEqual(summary['total_known_value'], D(1400))

    def test_confirmed_suspended_stock_without_t_price_keeps_unvalued_exposure(self):
        self.pcf = replace(self.pcf, components=tuple(replace(c, substitution_flag='1')
            if c.security_code == '000002' else c for c in self.components))
        del self.market['000002.SZ']
        self.write_market([(self.at - timedelta(minutes=1), '000002.SZ', 11.),
                           (self.at + timedelta(minutes=1), '000002.SZ', 12.)])
        data = self.build()
        row = next(row for row in data['rows'] if row['symbol'] == '000002.SZ')
        self.assertEqual(row['risk_tags'], 'suspension')
        self.assertIsNone(row['signal_value'])
        self.assertEqual(row['valuation_issue'], 'missing_exact_signal_close')
        self.assertEqual(data['trades'][0]['total_missing_value_count'], 1)
        self.assertIsNone(data['trades'][0]['total_value'])

    def test_zero_risk_trade_still_has_summary_and_missing_iopv_is_not_zero_weight(self):
        self.pcf = replace(self.pcf, components=(self.components[4],))
        data = self.build()
        self.assertEqual(data['rows'], [])
        self.assertEqual(len(data['trades']), 1)
        self.assertEqual(data['trades'][0]['total_value'], D(0))
        self.assertEqual(data['trades'][0]['total_weight'], D(0))
        missing = self.build(signals=[])
        self.assertIsNone(missing['trades'][0]['total_weight'])
        self.assertFalse(missing['trades'][0]['coverage_complete'])

    def test_unit_scaling_and_exports_roundtrip_with_cached_html(self):
        data = self.build([self.trade('T1', units=1), self.trade('T2', units=3)])
        self.assertEqual(data['schema_version'], 2)
        self.assertEqual(data['component_scope'], 'exclude_cross_market_cash_substitution')
        self.assertEqual([row['total_value'] for row in data['trades']], [D(1800), D(5400)])
        self.assertEqual([row['total_weight'] for row in data['trades']], [D('.18'), D('.18')])
        out = self.root / 'out'
        save_signal_risk(out, data)
        cached = load_signal_risk(out / 'signal_risk.json')
        self.assertEqual(signal_risk_html(cached), signal_risk_html(data))
        self.assertTrue((out / 'trade_signal_risk.csv').is_file())
        self.assertTrue((out / 'stock_signal_risk.csv').is_file())
        self.assertIn('信号时风险敞口', (out / 'signal_risk.html').read_text(encoding='utf-8'))
        self.assertNotIn('T2', signal_risk_html(cached, trade_id='T1'))
        bad = dict(cached, selection_basis='trade_open_legacy')
        (out / 'bad.json').write_text(json.dumps(bad), encoding='utf-8')
        with self.assertRaises(ValueError):
            load_signal_risk(out / 'bad.json')
        for incompatible in (dict(cached, schema_version=1),
                             dict(cached, component_scope='all_constituents')):
            with self.subTest(incompatible=incompatible.get('component_scope')):
                (out / 'bad.json').write_text(json.dumps(incompatible), encoding='utf-8')
                with self.assertRaises(ValueError):
                    load_signal_risk(out / 'bad.json')

    def configure_markets(self, code, market, *, price=11., unknown_cross_market=False):
        local_code, foreign_codes = (('000001', ('600000', '600001')) if market == 'SZ'
                                      else ('600000', ('000001', '000002')))
        local_source, foreign_source = ('102', '101') if market == 'SZ' else ('101', '102')
        foreign_market = 'SH' if market == 'SZ' else 'SZ'
        components = (
            PCFComponent(local_code, D(100), 'Local overlap', '必须现金替代',
                         creation_cash_substitute=D(500), market_code=local_source),
            PCFComponent(foreign_codes[0], D(100), 'Foreign allowed', '退补现金替代',
                         market_code=foreign_source),
            PCFComponent(foreign_codes[1], D(100), 'Foreign mandatory', '必须',
                         creation_cash_substitute=D(90000), market_code=foreign_source),
        )
        self.pcf = PCF(code, self.day, 1000, components, pre_trading_day=date(2024, 12, 31))
        self.config['etf_codes'] = [code]
        local = f'{local_code}.{market}'
        foreign = [f'{item}.{foreign_market}' for item in foreign_codes]
        self.market = {local: price, **{symbol: price for symbol in foreign}}
        if unknown_cross_market:
            self.market[foreign[0]] = None
            self.market[foreign[1]] = float('nan')
        self.write_market()
        with self.bounds.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.writer(stream)
            writer.writerow(['date', 'symbol', 'upper_limit', 'lower_limit'])
            writer.writerows([self.day, symbol, '11', '9']
                            for symbol in [local] + ([] if unknown_cross_market else foreign))
        self.rules = InMemoryRuleRepository(security_rules=[SecurityTradingRule(
            component.security_code, self.day, self.day, True, True, is_suspended=True)
            for component in components[:1 if unknown_cross_market else 3]])
        return local, foreign

    def test_all_supported_etf_markets_exclude_foreign_cash_in_every_risk_category(self):
        for code, market in (('159338', 'SZ'), ('159516', 'SZ'), ('512760', 'SH'),
                             ('512880', 'SH'), ('515880', 'SH')):
            for side, price, limit in (('CREATE', 11., 'limit_up'), ('REDEEM', 9., 'limit_down')):
                with self.subTest(code=code, side=side):
                    local, foreign = self.configure_markets(code, market, price=price)
                    data = self.build([self.trade(side=side, code=code, market=market)])
                    row, = data['rows']
                    self.assertEqual(row['symbol'], local)
                    self.assertEqual(row['risk_tags'], f'{limit};suspension;mandatory_cash')
                    summary, = data['trades']
                    self.assertEqual(summary['etf_market'], market)
                    self.assertEqual(summary['component_count'], 1)
                    self.assertEqual(summary['excluded_cross_market_cash_count'], 2)
                    for category in ('limit', 'suspension', 'mandatory_cash', 'total'):
                        self.assertEqual(summary[f'{category}_value'], D(1000))
                        self.assertEqual(summary[f'{category}_stock_count'], 1)
                        self.assertEqual(summary[f'{category}_unknown_stock_count'], 0)
                    # The denominator remains the entire ETF basket, not just retained stocks.
                    self.assertEqual(summary['basket_value'], D(20000))
                    self.assertEqual(summary['total_weight'], D('.05'))
                    self.assertTrue(summary['coverage_complete'])

    def test_excluded_foreign_cash_unknown_data_does_not_reduce_local_coverage(self):
        for code, market in (('159338', 'SZ'), ('512760', 'SH')):
            with self.subTest(code=code):
                local, foreign = self.configure_markets(code, market, unknown_cross_market=True)
                data = self.build([self.trade(code=code, market=market)])
                self.assertEqual([row['symbol'] for row in data['rows']], [local])
                summary, = data['trades']
                self.assertEqual(summary['total_value'], D(1000))
                self.assertEqual(summary['total_unknown_stock_count'], 0)
                self.assertEqual(summary['excluded_cross_market_cash_count'], 2)
                self.assertTrue(summary['coverage_complete'])
                self.assertEqual(data['quote_issue_count'], 0)
                self.assertTrue(all(symbol not in summary['unknown_details'] for symbol in foreign))

    def test_foreign_forbidden_or_unknown_substitution_is_not_silently_excluded(self):
        for flag, missing in (('禁止现金替代', False), ('unknown', True)):
            with self.subTest(flag=flag):
                self.pcf = replace(self.pcf, components=tuple(replace(component, substitution_flag=flag)
                    if component.security_code == '600000' else component for component in self.components))
                data = self.build([self.trade(side='REDEEM')])
                row = next(row for row in data['rows'] if row['symbol'] == '600000.SH')
                self.assertEqual(row['risk_tags'], 'limit_down')
                summary, = data['trades']
                self.assertEqual(summary['excluded_cross_market_cash_count'], 0)
                self.assertEqual(summary['component_count'], 5)
                if missing:
                    self.assertIsNone(row['signal_value'])
                    self.assertIsNone(summary['total_value'])
                    self.assertEqual(summary['mandatory_cash_unknown_stock_count'], 1)
                else:
                    self.assertEqual(row['signal_value'], D(1800))
                    self.assertEqual(summary['total_value'], D(3200))

    def test_unknown_etf_code_cannot_guess_its_market_from_a_symbol_suffix(self):
        self.pcf = replace(self.pcf, etf_code='999999')
        self.config['etf_codes'] = ['999999']
        with self.assertRaises(ValueError):
            self.build([self.trade(code='999999', market='SZ')])

    def test_missing_etf_symbol_uses_configured_code_and_wrong_suffix_is_rejected(self):
        trade = self.trade()
        trade.pop('etf_symbol')
        data = self.build([trade])
        self.assertEqual(data['trades'][0]['etf_market'], 'SZ')
        self.assertEqual(data['trades'][0]['excluded_cross_market_cash_count'], 1)
        for symbol in ('159338.SH', '159338.XX', '512760.SH'):
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                self.build([dict(self.trade(), etf_symbol=symbol)])

    def test_missing_fixed_cash_remains_unknown_instead_of_zero(self):
        self.pcf = replace(self.pcf, components=tuple(replace(c, creation_cash_substitute=None)
            if c.security_code == '000003' else c for c in self.components))
        data = self.build()
        row = next(row for row in data['rows'] if row['symbol'] == '000003.SZ')
        self.assertIsNone(row['signal_value'])
        self.assertEqual(row['valuation_issue'], 'missing_or_invalid_fixed_cash')
        self.assertIsNone(data['trades'][0]['mandatory_cash_value'])
        self.assertEqual(data['trades'][0]['mandatory_cash_known_value'], D(1000))
        self.assertEqual(data['trades'][0]['total_missing_value_count'], 1)

    def test_estimated_limit_source_is_retained_without_official_bounds(self):
        self.config['daily_price_limits_path'] = None
        previous = self.minute / '2024-12'
        previous.mkdir()
        symbols = list(self.market)
        pq.write_table(pa.table({'time': [datetime(2024, 12, 31, 15)] * len(symbols),
            'thscode': symbols, 'close': [10.] * len(symbols)}), previous / 'prior.parquet')
        data = self.build()
        row = next(row for row in data['rows'] if row['symbol'] == '000001.SZ')
        self.assertEqual(row['limit_source'], 'estimated')
        self.assertIn('前收盘估算', signal_risk_html(data))

    def test_unknown_substitution_kind_cannot_guess_stock_valuation(self):
        self.pcf = replace(self.pcf, components=tuple(replace(c, substitution_flag='unknown')
            if c.security_code == '000001' else c for c in self.components))
        data = self.build()
        row = next(row for row in data['rows'] if row['symbol'] == '000001.SZ')
        self.assertEqual(row['risk_tags'], 'limit_up')
        self.assertIsNone(row['signal_value'])
        self.assertEqual(row['valuation_basis'], 'unknown_substitution_flag')
        self.assertIsNone(data['trades'][0]['total_value'])
        self.assertEqual(data['trades'][0]['total_missing_value_count'], 1)
        self.assertIn('替代标志未确定', signal_risk_html(data))

    def test_bad_zero_share_mandatory_quote_does_not_cancel_fixed_cash(self):
        for price in (0., -1., float('nan'), float('inf'), None):
            with self.subTest(price=price):
                self.market['000003.SZ'] = price
                self.write_market()
                data = self.build()
                row = next(row for row in data['rows'] if row['symbol'] == '000003.SZ')
                self.assertEqual(row['signal_value'], D(400))
                self.assertIsNone(row['signal_price'])
                self.assertEqual(row['limit_status'], 'unknown')
                self.assertEqual(data['trades'][0]['total_value'], D(3600))
                self.assertEqual(data['quote_issue_count'], 1)

    def test_bad_real_stock_quote_is_unknown_without_inferred_suspension(self):
        self.market['000001.SZ'] = 0.
        self.write_market()
        data = self.build()
        self.assertNotIn('000001.SZ', {row['symbol'] for row in data['rows']})
        self.assertEqual(data['trades'][0]['limit_unknown_stock_count'], 1)
        self.assertEqual(data['trades'][0]['suspension_stock_count'], 1)
        self.assertIsNone(data['trades'][0]['total_value'])
        self.assertIn('invalid_close', data['trades'][0]['unknown_details'])

    def test_conflicting_exact_quotes_are_not_resolved_by_row_order(self):
        for first, second in ((11., 10.), (10., 11.)):
            with self.subTest(first=first):
                self.market['000001.SZ'] = first
                self.write_market([(self.at, '000001.SZ', second)])
                data = self.build()
                self.assertNotIn('000001.SZ', {row['symbol'] for row in data['rows']})
                self.assertEqual(data['trades'][0]['limit_unknown_stock_count'], 1)
                self.assertIsNone(data['trades'][0]['total_value'])
                self.assertIn('conflicting_close', data['trades'][0]['unknown_details'])


if __name__ == '__main__':
    unittest.main()
