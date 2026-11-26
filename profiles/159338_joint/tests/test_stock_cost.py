import csv
import hashlib
import json
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
import unittest

import pyarrow as pa
import pyarrow.parquet as pq
import test_full_flow as fixtures
from etf_arb.costs import FeeRates
from etf_arb.report.limit_impact import _stock_pnl, limit_impact_html
from etf_arb.report.stock_cost import cost_metrics, with_cost_metrics


class StockCostTests(unittest.TestCase):
    def test_cost_sign_denominators_and_fees_counted_once(self):
        result = cost_metrics('-12', '2', 2000, 10000)
        self.assertEqual(result['gross_price_pnl'], D('-10'))
        self.assertEqual(result['etf_cost_impact'], D('12'))
        self.assertEqual(result['etf_cost_impact_per_share'], D('.006'))
        self.assertEqual(result['etf_cost_impact_ratio'], D('.0012'))
        self.assertFalse(result['fee_only'])
        self.assertEqual(cost_metrics('8', '2', 2000, 10000)['etf_cost_impact'], D('-8'))

    def test_directional_html_uses_gross_prices_and_preserves_all_side_source(self):
        at = "2025-01-02 09:35"
        template = dict(opened_at=at, trade_id="T1", side="CREATE", quantity=100, etf_shares=1000,
            basket_value=10000, limit_stages="signal", limit_sources="provided", etf_weight=".10",
            signal_price=10, pricing_method="next_minute_close", execution_price=11,
            route="physical", security_name="", filled_quantity=100, valued_quantity=0,
            stock_pnl=-102, investor_stock_fees=2, manager_fees=0)
        # A ledger slice contains only rows and coverage. Its cached summary counts all directions.
        for side, direction, other in [("CREATE", "limit_up", "limit_down"),
                                       ("REDEEM", "limit_down", "limit_up")]:
            rows = [dict(template, side=side, symbol="000001.SZ", limit_types=direction),
                    dict(template, side=side, symbol="600000.SH", limit_types=other)]
            coverage = [dict(trade_id="T1", side=side, opened_at=at, limit_stock_count=2,
                             limit_symbols="000001.SZ;600000.SH", unknown_stock_count=1)]
            source = with_cost_metrics(dict(rows=rows, coverage=coverage))
            html = limit_impact_html(source, id_suffix="T1")
            self.assertIn("000001.SZ", html)
            self.assertNotIn("600000.SH", html)
            self.assertIn("<td>-100.00</td><td>100.00</td>", html)
            self.assertIn("<td>1</td><td>000001.SZ</td><td>1</td>", html)
            self.assertEqual(source["coverage"][0]["limit_stock_count"], 2)
            self.assertEqual(len(source["rows"]), 2)
            self.assertEqual(source["rows"][0]["etf_cost_impact"], D("102"))

    def test_hide_only_exact_fee_loss_not_small_or_unknown_pnl(self):
        self.assertTrue(cost_metrics('-2', 2, 100, 1000)['fee_only'])
        for pnl, fee in [('-1.999999', 2), (0, 0), (None, 2), (-2, None)]:
            self.assertFalse(cost_metrics(pnl, fee, None, None)['fee_only'])
        unknown = cost_metrics(None, 2, 100, 1000)
        self.assertIsNone(unknown['etf_cost_impact'])
        missing = cost_metrics(-2, 2, 0, None)
        self.assertIsNone(missing['etf_cost_impact_per_share'])
        self.assertIsNone(missing['etf_cost_impact_ratio'])

    def test_legacy_rows_recover_shares_without_mutation(self):
        row = dict(trade_id='T', stock_pnl='-2', manager_fees='2',
                   investor_stock_fees='0', basket_value='1000')
        source = dict(rows=[row], coverage=[], events=[])
        enriched = with_cost_metrics(source, [dict(trade_id='T', etf_shares='200')])
        self.assertNotIn('fee_only', row)
        self.assertEqual(enriched['rows'][0]['etf_cost_impact_per_share'], D('.01'))
        self.assertTrue(enriched['rows'][0]['fee_only'])
        self.assertEqual(len(enriched['rows']), len(source['rows']))

    def test_creation_redemption_partial_pricing_and_pending_settlement(self):
        for side, fill_price, expected in [('CREATE', '11', '-102'), ('REDEEM', '9', '-102')]:
            trade = dict(trade_id='T', side=side, quantity_units=1, applied_at='2025-01-02 09:31')
            plan = SimpleNamespace(manager={'600000.SH': 100}, physical={})
            fill = dict(purpose='MANAGER', side='BUY' if side == 'CREATE' else 'SELL',
                        quantity=100, price=fill_price, ts='2025-01-02 09:32')
            fee = dict(purpose='MANAGER', side=fill['side'], total='2')
            result = _stock_pnl(trade, None, plan, '600000.SH', D(10), [fill], [fee], [])
            self.assertEqual(result['stock_pnl'], D(expected))
            self.assertEqual(cost_metrics(result['stock_pnl'], result['manager_fees'], 1000, 10000)['etf_cost_impact'], D(102))
            partial = _stock_pnl(trade, None, plan, '600000.SH', D(10),
                                 [dict(fill, quantity=50)], [fee], [])
            self.assertIsNone(partial['stock_pnl'])


class StockCostFullPathTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.FullFlowTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def run_case(self):
        case = self.case
        previous = case.market / '2024-12'
        previous.mkdir()
        pq.write_table(pa.table({'time': [datetime(2024, 12, 31, 15)] * 2,
            'thscode': ['000001.SZ', '600000.SH'], 'close': [1.0, 2.0]}), previous / 'prior.parquet')
        rows = [(datetime.combine(day, clock), symbol, price)
                for day in fixtures.DAYS for clock in fixtures.CLOCKS
                for symbol, price in [('159338.SZ', 4.1), ('000001.SZ', 1.1),
                    ('600000.SH', 2.2 if clock == time(9, 30) else 2.1)]]
        config = replace(case.config, simulation=replace(case.config.simulation,
            quantity_units=2, fees=FeeRates()))
        runner, summary = case.run_model(rows, config)
        return config, runner, summary

    def test_runner_exports_all_rows_but_hides_fee_only_html_detail(self):
        config, runner, summary = self.run_case()
        root = config.output_dir
        data = json.loads((root / 'stock_limit_impact.json').read_text(encoding='utf-8'))
        stocks = {row['symbol']: row for row in data['rows']}
        self.assertEqual(set(stocks), {'000001.SZ', '600000.SH'})
        self.assertTrue(stocks['000001.SZ']['fee_only'])
        self.assertFalse(stocks['600000.SH']['fee_only'])
        for row in stocks.values():
            self.assertEqual(D(row['etf_cost_impact']), -D(row['stock_pnl']))
            self.assertEqual(D(row['etf_cost_impact_per_share']), -D(row['stock_pnl']) / 200)
            self.assertEqual(D(row['etf_cost_impact_ratio']), -D(row['stock_pnl']) / D(row['basket_value']))
        html = limit_impact_html(data)
        detail = html.split('<tbody>', 1)[1].split('</tbody>', 1)[0]
        self.assertNotIn('000001.SZ', detail)
        self.assertIn('600000.SH', detail)
        self.assertIn('明细已隐藏 1 条', html)
        for filename in ['report/report.html', 'report/ledger.html']:
            self.assertIn('每份ETF成本影响', (root / filename).read_text(encoding='utf-8'))
        def read(name):
            with (root / name).open(encoding='utf-8-sig', newline='') as stream:
                return list(csv.DictReader(stream))
        self.assertEqual(read('stock_limit_impact.csv'), read('report/stock_limit_impact.csv'))
        self.assertEqual(len(read('stock_limit_impact.csv')), 2)
        self.assertEqual(len(read('portfolio_ledger.csv')[0]), 6)
        self.assertEqual(sum((D(r['cash_delta']) for r in read('cashflows.csv')), D(0)), D(summary['final_net_pnl']))
        self.assertTrue(any(f['symbol'] == '000001.SZ' for f in runner.executor.fills))
        self.assertGreater(sum((D(r['total']) for r in read('fees.csv') if r['symbol'] == '000001.SZ'), D(0)), 0)

    def test_saved_run_rebuild_keeps_originals_and_cost_fields(self):
        from etf_arb.report.rebuild import rebuild_run_report
        config, _, _ = self.run_case()
        from etf_arb.report.stock_cost import COST_FIELDS
        saved = config.output_dir / 'stock_limit_impact.json'
        legacy = json.loads(saved.read_text(encoding='utf-8'))
        for row in legacy['rows']:
            for field in COST_FIELDS:
                row.pop(field, None)
        saved.write_text(json.dumps(legacy), encoding='utf-8')
        # Cached reports must upgrade without replaying fills or reading raw market data.
        config.pcf_root.rename(self.case.root / 'pcf-unavailable')
        config.minute_root.rename(self.case.root / 'market-unavailable')
        hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in config.output_dir.rglob('*') if p.is_file()}
        output = self.case.root / 'rebuilt'
        rebuild_run_report(config.output_dir, output)
        self.assertEqual(hashes, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in hashes})
        self.assertIn('每份ETF成本影响', (output / 'report.html').read_text(encoding='utf-8'))
        with (output / 'stock_limit_impact.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['etf_shares'] for r in rows}, {'200'})


if __name__ == '__main__':
    unittest.main()
