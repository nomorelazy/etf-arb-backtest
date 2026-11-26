"""Exercise the approved template through the normal runner and saved-report CLI."""

from contextlib import redirect_stdout
import csv
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from html.parser import HTMLParser
from io import StringIO
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import pyarrow as pa
import pyarrow.parquet as pq
import test_full_flow as fixtures
from etf_arb.cli import main
from etf_arb.report.limit_exposure import build_from_saved_records, limit_exposure_html


class ReportHeadings(HTMLParser):
    def __init__(self):
        super().__init__()
        self.h2 = []
        self.current = None
        self.scroll_regions = []
        self.ids = set()
        self.detail_controls = []

    def handle_starttag(self, tag, attrs):
        if tag == 'h2':
            self.current = []
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.add(attrs['id'])
        if 'lv-scroll' in attrs.get('class', '').split():
            self.scroll_regions.append(attrs)
        if tag == 'button' and 'lv-inspect' in attrs.get('class', '').split():
            self.detail_controls.append(attrs.get('aria-controls'))

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == 'h2' and self.current is not None:
            self.h2.append(''.join(self.current))
            self.current = None


class ApprovedTemplateTests(unittest.TestCase):
    def assert_sections(self, report):
        headings = ReportHeadings()
        headings.feed(report)
        title = "信号时涨跌停股票总市值及 ETF 净值占比"
        self.assertEqual(headings.h2.count(title), 1)
        self.assertEqual(headings.h2[-1], title)
        order = ["逐笔价差与现金盈亏", "涨跌停延迟成交与敞口（不含税费，按发起时间排列）",
                 "申购涨停／赎回跌停股票价格影响（不含税费）", title]
        self.assertEqual([h for h in headings.h2 if h in order], order)
        self.assertEqual(report.count('id="limit-values"'), 1)
        self.assertIn('href="#limit-values"', report)
        self.assertIn('href="limit_value.csv"', report)
        for heading in ("深市方向性敞口", "沪市方向性敞口", "沪深方向性合计", "本市场投资者敞口", "本市场口径涨跌停敞口／占比"):
            self.assertIn(heading, report)
        scroll, = headings.scroll_regions
        self.assertEqual(scroll.get('role'), 'region')
        self.assertEqual(scroll.get('tabindex'), '0')
        self.assertTrue({'lv-values', 'lv-side', 'lv-filter'} <= headings.ids)
        self.assertTrue(headings.detail_controls)
        self.assertTrue(all(target in headings.ids for target in headings.detail_controls))
        self.assertFalse({'lv-prev', 'lv-page', 'lv-next'} & headings.ids)
        self.assertRegex(report, r'\.lv-scroll\s*\{[^}]*\boverflow(?:-y)?\s*:\s*(auto|scroll)')
        self.assertRegex(report, r'\.lv-scroll\s*\{[^}]*max-(height|block-size)\s*:')
        self.assertRegex(report, r'thead[^{}]*\{[^}]*position\s*:\s*sticky')

    def test_runner_and_offline_cli_use_same_snapshot_and_six_column_ledger(self):
        case = fixtures.FullFlowTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        prior = case.market / '2024-12'
        prior.mkdir()
        pq.write_table(pa.table(dict(time=[datetime(2024, 12, 31, 15)] * 2,
            thscode=['000001.SZ', '600000.SH'], close=[1.0, 2.0])), prior / 'close.parquet')
        # At the signal the SZ component is limit up, the cross-market SH component is limit down.
        quotes = [(at, symbol, {'000001.SZ': 1.1, '600000.SH': 1.8}.get(symbol, price))
                  for at, symbol, price in case.market_rows(4.1, 4.1)]
        config = replace(case.config, simulation=replace(case.config.simulation, quantity_units=2))
        _, summary = case.run_model(quotes, config)
        run = config.output_dir
        raw_report = (run / 'report/report.html').read_text(encoding='utf-8')
        self.assert_sections(raw_report)
        payload = json.loads((run / 'limit_value.json').read_text(encoding='utf-8'))
        row, = payload['rows']
        self.assertEqual((row['side'], Decimal(row['etf_shares'])), ('CREATE', 200))
        self.assertEqual(Decimal(row['all']['up']['value']), 220)
        self.assertEqual(Decimal(row['all']['down']['value']), 360)
        self.assertEqual(Decimal(row['all']['total']['value']), 580)
        self.assertEqual(Decimal(row['local_directional']['value']), 220)
        basket = Decimal(row['iopv']) * 200
        self.assertEqual(Decimal(row['basket_value']), basket)
        self.assertEqual(Decimal(row['all']['total']['weight']), Decimal(580) / basket)
        self.assertEqual(Decimal(row['local_directional']['weight']), Decimal(220) / basket)
        with (run / 'portfolio_ledger.csv').open(encoding='utf-8-sig', newline='') as handle:
            header, record = list(csv.reader(handle))
        self.assertEqual(len(header), 6)
        self.assertEqual(len(record), 6)
        with (run / 'trades.csv').open(encoding='utf-8-sig', newline='') as handle:
            trade, = csv.DictReader(handle)
        self.assertEqual(Decimal(trade['net_pnl']), Decimal(summary['final_net_pnl']))
        protected = {name: (run / name).read_bytes() for name in
                     ('portfolio_ledger.csv', 'fills.csv', 'cashflows.csv', 'trades.csv', 'limit_value.json')}
        case.pcf.rename(case.root / 'pcf-offline')
        case.market.rename(case.root / 'quotes-offline')
        output = case.root / 'rebuilt'
        stdout = StringIO()
        with patch('sys.argv', ['etf_arb', 'report-attribution', '--run-dir', str(run),
                                '--output', str(output)]), redirect_stdout(stdout):
            code = main()
        self.assertEqual(code, 0, stdout.getvalue())
        self.assert_sections((output / 'report.html').read_text(encoding='utf-8'))
        self.assertEqual(json.loads((output / 'limit_value.json').read_text(encoding='utf-8')), payload)
        for name, before in protected.items():
            self.assertEqual((run / name).read_bytes(), before)


class SavedLimitBlockTests(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.at = datetime(2025, 1, 2, 9, 35)
        self.applied = self.at.replace(minute=36)
        self.config = dict(minute_root=tmp.name, end_date="2025-01-03", etf_codes=["159338"],
                           simulation=dict(one_minute_execution=True))
        self.trade = dict(trade_id="T1", side="REDEEM", etf_shares=1000, opened_at=self.at,
            applied_at=self.applied, delayed_redemption_components='{"000001.SZ":"limit_down:provided"}')
        self.order = dict(trade_id="T1", order_id="O1", symbol="000001.SZ", purpose="MANAGER",
                          side="SELL", quantity=100, submitted=self.applied)
        self.attempt = dict(trade_id="T1", order_id="O1", ts=self.applied, requested_quantity=100,
                            unfilled_quantity=100, reason="limit_down:provided")
        self.fill = dict(order_id="O1", ts=self.at.replace(minute=40), price="9.50", quantity=100)
        self.risk = dict(trade_id="T1", entry_at=self.applied, reference_at=self.applied,
                         quantity=100, reference_price="9", symbol="000001.SZ", reason="limit_down:provided")

    def build(self, *, risks=(), blocks=None, attempts=()):
        return build_from_saved_records(self.config, [self.trade], [self.order], [self.fill],
            [], [], [dict(ts=self.at, iopv=1)], attempts=attempts, execution_blocks=blocks,
            redemption_risks=risks, stock_rows=[dict(trade_id="T1", symbol="000001.SZ", security_name="样本")])

    def test_partial_sources_agree_on_one_observation_without_double_counting(self):
        data = self.build(risks=[self.risk], attempts=[self.attempt, self.attempt])
        row, = data["rows"]
        self.assertEqual(row["observed_limit_minutes"], 1)
        self.assertEqual(row["planned_at"], self.applied)
        self.assertEqual(row["period_price_pnl"], Decimal("50"))
        self.assertEqual(row["etf_cost_impact"], Decimal("-50"))
        self.assertIn("受阻记录覆盖不完整", limit_exposure_html(data))

    def test_partial_missing_exact_block_price_stays_unknown(self):
        row, = self.build(attempts=[self.attempt])["rows"]
        self.assertIsNone(row["blocked_price"])
        self.assertIsNone(row["initial_price"])
        self.assertIsNone(row["period_price_pnl"])
        self.assertEqual(row["filled_quantity"], 100)

    def test_late_fill_is_not_block_evidence_and_empty_full_log_is_authoritative(self):
        data = self.build(risks=[self.risk], attempts=[self.attempt], blocks=[])
        self.assertEqual(data["rows"], [])
        self.assertNotIn("受阻记录覆盖不完整", limit_exposure_html(data))
        self.trade["delayed_redemption_components"] = "000001.SZ"
        self.assertEqual(self.build(attempts=[dict(self.attempt, reason="missing_exact_bar")])["rows"], [])

    def test_partial_quantity_and_checks_at_or_before_observation_cutoff(self):
        trade = dict(self.trade, delayed_redemption_components={})
        at = self.applied
        cutoff = at.replace(minute=39)
        first = dict(self.attempt, requested_quantity="", filled_quantity=20, unfilled_quantity=80)
        late = dict(self.attempt, ts=at.replace(minute=41), unfilled_quantity=0)
        fills = [dict(self.fill, ts=at, quantity=20, price=9), dict(self.fill, quantity=80)]
        data = build_from_saved_records(self.config, [trade], [self.order], fills, [], [],
            [dict(ts=self.at, iopv=1)], attempts=[first, late, dict(first, ts=at.replace(minute=42))],
            observed_through=cutoff, redemption_risks=[self.risk])
        row, = data["rows"]
        self.assertEqual((row["filled_quantity"], row["unpriced_quantity"]), (20, 80))
        self.assertEqual(row["observed_limit_minutes"], 1)
        self.assertEqual(row["exposure_end_at"], cutoff)
        self.assertIsNone(row["period_price_pnl"])
