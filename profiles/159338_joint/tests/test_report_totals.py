from datetime import datetime
from decimal import Decimal as D
from html.parser import HTMLParser
import unittest

from etf_arb.report.attribution import REPORT_COLUMNS, attribution_html, attribution_totals


class FooterParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.footers = {}
        self.direction_pnl = {}
        self.summary_id = None
        self.current = None
        self.cell = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'span' and attrs.get('id') in ('creation-net-pnl', 'redemption-net-pnl'):
            self.summary_id = attrs['id']
            self.direction_pnl[self.summary_id] = ''
        if tag == 'tr' and 'data-summary-filter' in attrs:
            self.current = attrs['data-summary-filter']
            self.footers[self.current] = []
        if tag == 'td' and self.current is not None:
            self.cell = ''

    def handle_data(self, data):
        if self.summary_id is not None:
            self.direction_pnl[self.summary_id] += data
        if self.cell is not None:
            self.cell += data

    def handle_endtag(self, tag):
        if tag == 'span':
            self.summary_id = None
        if tag == 'td' and self.cell is not None:
            self.footers[self.current].append(self.cell)
            self.cell = None
        if tag == 'tr':
            self.current = None


class ReportTotalsTests(unittest.TestCase):
    def rows(self):
        base = {field: D('0') for field in REPORT_COLUMNS}
        base.update(opened_at=datetime(2025, 1, 2, 10), signal_premium_rate=D('.02'))
        return [base | dict(trade_id='T1', side='CREATE', net_pnl=D('-10.004'),
                            cash_substitution_pnl=D('2.004'), cash_substitution_valued_quantity=2),
                base | dict(trade_id='T2', side='REDEEM', net_pnl=D('20.016'),
                            cash_substitution_pnl=D('3.004'), cash_substitution_valued_quantity=3),
                base | dict(trade_id='T3', side='CREATE', net_pnl=None,
                            cash_substitution_pnl=None, cash_substitution_valued_quantity=5)]

    def test_exact_money_totals_and_missing_amounts_are_not_zero_profit(self):
        total = attribution_totals(self.rows())
        self.assertEqual(total['最终净盈亏（元）'], '10.01（已知合计；1 笔未确定）')
        self.assertEqual(total['现金替代价格盈亏（元，含代理费）'], '5.01（已知合计；1 笔未确定）')
        self.assertEqual(total['未成交估值数量'], '10')
        self.assertEqual(total['信号折溢价率'], '—')
        self.assertEqual(total['发起时间'], '共 3 笔')
        pending = attribution_totals(self.rows()[2:])
        self.assertEqual(pending['最终净盈亏（元）'], '未确定（1 笔）')
        self.assertEqual(pending['现金替代价格盈亏（元，含代理费）'], '未确定（1 笔）')
        self.assertEqual(attribution_totals([])['最终净盈亏（元）'], '0.00')

    def test_html_filter_totals_match_the_trades_in_each_direction(self):
        html = attribution_html(self.rows())
        self.assertIn('申购 2 笔，赎回 1 笔，合计 3 笔', html)
        parser = FooterParser(html)
        parsed = parser.footers
        self.assertEqual(set(parsed), {'all', 'loss', 'create', 'redeem'})
        net_column = list(REPORT_COLUMNS).index('net_pnl')
        cash_column = list(REPORT_COLUMNS).index('cash_substitution_pnl')
        self.assertEqual(parsed['loss'][net_column], '-10.00')
        self.assertEqual(parsed['create'][net_column], '-10.00（已知合计；1 笔未确定）')
        self.assertEqual(parsed['redeem'][net_column], '20.02')
        self.assertEqual(parsed['all'][net_column], '10.01（已知合计；1 笔未确定）')
        self.assertEqual(parsed['all'][cash_column], '5.01（已知合计；1 笔未确定）')
        self.assertEqual(len(parsed['all']), len(REPORT_COLUMNS))
        self.assertEqual(parser.direction_pnl, {
            'creation-net-pnl': '-10.00（已知合计；1 笔未确定）',
            'redemption-net-pnl': '20.02',
        })
        self.assertEqual(parser.direction_pnl['creation-net-pnl'], parsed['create'][net_column])
        self.assertEqual(parser.direction_pnl['redemption-net-pnl'], parsed['redeem'][net_column])

    def test_direction_pnl_nets_wins_and_losses_before_rounding(self):
        base = self.rows()[0]
        rows = [base | dict(trade_id='T1', side='CREATE', net_pnl='10.004'),
                base | dict(trade_id='T2', side='CREATE', net_pnl='-2.996'),
                base | dict(trade_id='T3', side='REDEEM', net_pnl='-20.004'),
                base | dict(trade_id='T4', side='REDEEM', net_pnl='3.006')]
        self.assertEqual(FooterParser(attribution_html(rows)).direction_pnl, {
            'creation-net-pnl': '7.01', 'redemption-net-pnl': '-17.00',
        })
        self.assertEqual(FooterParser(attribution_html(self.rows()[2:])).direction_pnl, {
            'creation-net-pnl': '未确定（1 笔）', 'redemption-net-pnl': '0.00',
        })

    def test_no_trades_report_zero_direction_counts(self):
        html = attribution_html([])
        self.assertIn('申购 0 笔，赎回 0 笔，合计 0 笔', html)
        self.assertIn('本轮没有已发起交易', html)
        self.assertEqual(FooterParser(html).direction_pnl, {
            'creation-net-pnl': '0.00', 'redemption-net-pnl': '0.00',
        })


if __name__ == '__main__':
    unittest.main()
