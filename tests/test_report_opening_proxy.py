"""Reports distinguish opening-close proxies from prior-close estimates."""

from datetime import date, datetime
from decimal import Decimal as D
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq

from etf_arb.domain import PCF, PCFComponent
from etf_arb.report.limit_impact import DETAIL_FIELDS, limit_impact_html
from etf_arb.report.signal_risk import build_signal_risk, signal_risk_html
from etf_arb.rules import InMemoryRuleRepository, SecurityTradingRule


PROXY = 'estimated_open_0930_proxy'


class OpeningProxyReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.day, self.at = date(2025, 1, 2), datetime(2025, 1, 2, 9, 35)
        components = tuple(PCFComponent(code, D(100), code, '1', market_code='102')
                           for code in ('000001', '000002'))
        self.pcf = PCF('159338', self.day, 1000, components, pre_trading_day=date(2024, 12, 31))
        self.rules = InMemoryRuleRepository(security_rules=[SecurityTradingRule(
            c.security_code, self.day, self.day, True, True) for c in components])
        self.config = dict(pcf_root=str(self.root / 'pcf'), minute_root=str(self.root),
                           etf_codes=['159338'], end_date='2025-01-02')
        self.trade = dict(trade_id='T1', opened_at=self.at, side='CREATE', quantity_units=1,
                          etf_shares=1000, etf_symbol='159338.SZ')
        (self.root / 'pcf').mkdir()
        for month, records in {
            '2024-12': [(datetime(2024, 12, 31, 15), '000002.SZ', 10.)],
            '2025-01': [(datetime(2025, 1, 2, 9, 30), symbol, 10.)
                        for symbol in ('000001.SZ', '000002.SZ')]
                       + [(self.at, symbol, 11.) for symbol in ('000001.SZ', '000002.SZ')],
        }.items():
            folder = self.root / month
            folder.mkdir()
            pq.write_table(pa.table({'time': [r[0] for r in records],
                'thscode': [r[1] for r in records], 'close': [r[2] for r in records]}),
                folder / 'quotes.parquet')

    def test_mixed_estimated_sources_count_and_render_separately(self):
        with patch('etf_arb.report.signal_risk.FilePCFRepository.get', return_value=self.pcf):
            data = build_signal_risk(self.config, [self.trade],
                [dict(ts=self.at, iopv='10')], rules=self.rules)
        self.assertEqual({r['symbol']: r['limit_source'] for r in data['rows']},
                         {'000001.SZ': PROXY, '000002.SZ': 'estimated'})
        self.assertEqual(data['trades'][0]['estimated_limit_stock_count'], 2)
        html = signal_risk_html(data)
        self.assertIn('09:30 close 开盘代理估算', html)
        self.assertIn('前收盘估算', html)
        self.assertIn('不是真实开盘价或交易所限价', html)
        self.assertIn('估算限价 2 只', html)

    def test_stock_impact_sources_are_mapped_as_whole_identifiers(self):
        row = dict.fromkeys(DETAIL_FIELDS)
        row.update(trade_id='T1', opened_at=self.at, side='CREATE', symbol='000001.SZ',
            security_name='样本', quantity=100, etf_shares=1000, basket_value=10000,
            limit_types='limit_up', limit_stages='signal', limit_sources=PROXY + ';estimated',
            route='physical', pricing_method='signal_minute_close', filled_quantity=100,
            valued_quantity=0, signal_price=11, execution_price=D('11.1'),
            stock_pnl=-10, manager_fees=0, investor_stock_fees=0, etf_weight=D('.11'))
        html = limit_impact_html(dict(rows=[row], coverage=[]))
        self.assertIn('／09:30 close 开盘代理估算；前收盘估算</td>', html)
        self.assertNotIn('推算_open_0930_proxy', html)
        self.assertIn('不是真实开盘价或交易所限价', html)

    @unittest.skipUnless(find_spec('etf_arb.report.limit_exposure'), 'This version has no exposure report')
    def test_recorded_exposure_keeps_proxy_source_visible_without_changing_pnl(self):
        from etf_arb.report.limit_exposure import build_limit_exposure, limit_exposure_html
        order = dict(order_id='O1', trade_id='T1', symbol='000001.SZ', side='BUY',
                     purpose='MANAGER', quantity=100, submitted=self.at)
        event = dict(order, ts=self.at, unfilled_quantity=100, close=11,
                     reason='stock_limit_up:' + PROXY)
        fill = dict(order_id='O1', ts=datetime(2025, 1, 2, 9, 36), quantity=100, price=D('11.2'))
        with patch('etf_arb.report.limit_exposure.FilePCFRepository.get', return_value=self.pcf):
            data = build_limit_exposure(self.config, [self.trade], [order], [fill], [], [],
                [event], [dict(ts=self.at, iopv='10')], rules=self.rules)
        self.assertEqual(data['rows'][0]['limit_sources'], PROXY)
        self.assertEqual(data['rows'][0]['period_price_pnl'], D('-20'))
        html = limit_exposure_html(data)
        self.assertIn('涨停<br>09:30 close 开盘代理估算', html)
        self.assertIn('不是真实开盘价或交易所限价', html)


if __name__ == '__main__':
    unittest.main()
