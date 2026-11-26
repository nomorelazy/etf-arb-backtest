import csv
import json
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal as D

import test_full_flow as fixtures
from etf_arb.costs import FeeRates


class RedemptionLimitDownTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.FullFlowTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.days = fixtures.DAYS
        self.bounds = self.case.root / 'limits.csv'

    def run_case(self, rows, *, split=True, remove_after_day_one=False, cap=None):
        if remove_after_day_one:
            for day in self.days[1:]:
                path = self.case.pcf / '159338' / f'{day:%Y%m%d}.xml'
                tree = ET.parse(path)
                components = tree.find('Components')
                for c in list(components):
                    if c.findtext('UnderlyingSecurityID') in {'600000', '000001'}:
                        components.remove(c)
                tree.write(path, encoding='utf-8')
        cfg = self.case.config
        return self.case.run_model(rows, replace(cfg, daily_price_limits_path=self.bounds,
            simulation=replace(cfg.simulation, market_split_execution=split, fees=FeeRates(),
                               max_shares_per_minute=cap, max_trades_per_day=1)))

    def write_bounds(self, per_day):
        with self.bounds.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['date', 'symbol', 'upper_limit', 'lower_limit', 'no_limit'])
            for day, first, second in zip(self.days, *per_day):
                for symbol, limits in [('000001.SZ', first), ('600000.SH', second)]:
                    writer.writerow([day, symbol, *limits] if limits else [day, symbol, '', '', 'true'])

    def test_waits_through_unknown_day_after_stocks_leave_the_pcf(self):
        # Both legs start locked. Day two quotes are outside the supplied bounds
        # and cannot establish that the stock has unlocked. Day three has no limit.
        self.write_bounds(([(2, 1), (3, 1.5), None], [(3, 2), (4, 2.5), None]))
        runner, summary = self.run_case(self.case.market_rows(3.2, 3.1), remove_after_day_one=True)
        sim = runner.executor
        self.assertEqual([D('100'), D('100'), D('0')], [day['holdings_value'] for day in sim.daily])
        start = datetime.combine(self.days[0], time(9, 31))
        renewed = datetime.combine(self.days[2], time(9, 30))
        stock_fills = [f for f in sim.fills if f['side'] == 'SELL']
        self.assertEqual({(f['symbol'], f['purpose'], f['ts'], f['quantity']) for f in stock_fills},
                         {('000001.SZ', 'EXIT', renewed, 100), ('600000.SH', 'MANAGER', renewed, 100)})
        self.assertEqual(len(stock_fills), 2)
        self.assertEqual(sim.trades[0].delayed_redemption,
                         {'600000.SH': 'limit_down:provided', '000001.SZ': 'limit_down:provided'})
        self.assertEqual(sim.substitution_attempts[0]['ts'], start)
        self.assertEqual(len({(b['order_id'], b['ts']) for b in sim.execution_limit_blocks}),
                         len(sim.execution_limit_blocks))
        self.assertEqual(summary['settled_count'], 1)

    def test_stock_retained_but_unfilled_proxy_valued_at_deadline(self):
        self.write_bounds(([(2, 1)] * 3, [(3, 2)] * 3))
        runner, summary = self.run_case(self.case.market_rows(3.2, 3.1))
        sim = runner.executor
        self.assertFalse([f for f in sim.fills if f['side'] == 'SELL'])
        self.assertFalse([f for f in sim.fee_rows if f['side'] == 'SELL'])
        self.assertEqual(sim.account.holdings('T000001'), {'000001.SZ': 100})
        self.assertEqual(sim.daily[-1]['holdings_value'], D('100'))
        self.assertEqual(summary['pending_count'], 1)
        self.assertEqual([(v['symbol'], v['quantity'], v['price']) for v in sim.substitution_pricing],
                         [('600000.SH', 100, D('2'))])
        with (self.case.config.output_dir / 'limit_exposure.json').open(encoding='utf-8') as stream:
            exposure = {r['symbol']: r for r in json.load(stream)['rows']}
        self.assertEqual(exposure['000001.SZ']['unpriced_quantity'], 100)
        self.assertEqual(exposure['600000.SH']['valued_quantity'], 100)
        self.assertEqual(exposure['000001.SZ']['filled_quantity'], exposure['600000.SH']['filled_quantity'])
        self.assertEqual(exposure['600000.SH']['filled_quantity'], 0)

    def test_deadline_locks_each_redeemed_stock_while_another_has_no_exact_close(self):
        self.case.config = replace(self.case.config, quality_check=False, simulation=replace(
            self.case.config.simulation, cash_substitution_pricing_days=1))
        for day in self.days:
            path = self.case.pcf / '159338' / f'{day:%Y%m%d}.xml'
            tree = ET.parse(path)
            extra = ET.fromstring('<Component><UnderlyingSecurityID>600005</UnderlyingSecurityID>'
                                  '<UnderlyingSecurityIDSource>101</UnderlyingSecurityIDSource>'
                                  '<ComponentShare>100</ComponentShare><SubstituteFlag>1</SubstituteFlag></Component>')
            tree.find('Components').append(extra)
            tree.write(path, encoding='utf-8')
        self.write_bounds(([(2, 0.5)] * 3, [(3, 2)] * 3))
        with self.bounds.open('a', encoding='utf-8', newline='') as stream:
            csv.writer(stream).writerows([day, '600005.SH', '4', '3', ''] for day in self.days)
        rows = []
        cutoff = datetime.combine(self.days[1], time(15))
        for ts, symbol, price in self.case.market_rows(3.2, 3.1):
            if symbol == '600000.SH':
                if ts != cutoff:
                    rows.append((ts, '600005.SH', 3))
                if ts.date() == self.days[-1]:
                    price = 8
            rows.append((ts, symbol, price))
        runner, summary = self.run_case(rows)
        sim = runner.executor
        sub = next(leg for leg in sim.trades[0].legs if leg.kind == 'cash_substitution')
        self.assertEqual(sub.estimate, D('550'))  # 200 locked, 300 pending, 50 fixed.
        self.assertIsNone(sub.final)
        self.assertIsNone(sub.priced_at)
        self.assertFalse(sub.paid)
        self.assertEqual(summary['pending_count'], 1)
        self.assertFalse(any(fill['purpose'] == 'MANAGER' for fill in sim.fills))
        self.assertFalse(any(fee['charged_to'] != 'investor_cash' for fee in sim.fee_rows))
        with (self.case.config.output_dir / 'substitution_pricing.csv').open(encoding='utf-8-sig') as stream:
            valuations = list(csv.DictReader(stream))
        self.assertEqual([(r['symbol'], r['quantity'], D(r['price']), r['ts']) for r in valuations],
                         [('600000.SH', '100', D('2'), str(cutoff))])
        with (self.case.config.output_dir / 'limit_exposure.json').open(encoding='utf-8') as stream:
            exposure = {r['symbol']: r for r in json.load(stream)['rows']}
        self.assertEqual(exposure['600000.SH']['valued_quantity'], 100)
        self.assertEqual(exposure['600005.SH']['unpriced_quantity'], 100)

    def sparse_retry(self, signal_time, unlock, *, split=True):
        day = self.days[0]
        signal = datetime.combine(day, signal_time)
        step = timedelta(minutes=1 if split else 5)
        application = signal + step
        clocks = tuple(sorted({signal.time(), application.time(), (application + timedelta(minutes=5)).time(),
                               time(15)}))
        self.case.config = replace(self.case.config, clock_times=clocks, quality_check=False,
            simulation=replace(self.case.config.simulation,
                               trading_sessions=(('09:35', '11:29'), ('13:03', '14:57'))))
        self.write_bounds(([(2, 1)] * 3, [(3, 2)] * 3))
        rows = []
        for market_day in self.days:
            times = {datetime.combine(market_day, clock) for clock in clocks}
            if market_day == unlock.date():
                times.add(unlock)
                times.add(unlock - timedelta(minutes=1))
            for at in sorted(times):
                rows.extend([(at, '159338.SZ', 3.2),
                             (at, '000001.SZ', 1 if at < unlock else 1.1),
                             (at, '600000.SH', 2 if at < unlock else 2.1)])
        runner, _ = self.run_case(rows, split=split)
        self.assertEqual(runner.executor.trades[0].applied_at, application)
        return runner.executor

    def test_redeemed_stock_and_proxy_unlock_at_lunch_reopen_before_signal_window(self):
        unlock = datetime.combine(self.days[0], time(13))
        sim = self.sparse_retry(time(11, 28), unlock)
        self.assertCountEqual([(f['purpose'], f['ts']) for f in sim.fills if f['side'] == 'SELL'],
                              [('MANAGER', unlock), ('EXIT', unlock)])

    def test_both_stock_sales_resume_at_next_open_outside_runner_clocks(self):
        unlock = datetime.combine(self.days[1], time(9, 30))
        sim = self.sparse_retry(time(14, 52), unlock)
        self.assertCountEqual([(f['purpose'], f['ts']) for f in sim.fills if f['side'] == 'SELL'],
                              [('MANAGER', unlock), ('EXIT', unlock)])

    def test_five_minute_legacy_clock_checks_domestic_sales_each_minute_after_first_block(self):
        unlock = datetime.combine(self.days[0], time(9, 46))
        sim = self.sparse_retry(time(9, 35), unlock, split=False)
        self.assertEqual([(f['purpose'], f['ts']) for f in sim.fills if f['side'] == 'SELL'
                          and f['purpose'] == 'EXIT'], [('EXIT', unlock)])
        self.assertEqual(sim.substitution_attempts[0]['ts'],
                         datetime.combine(self.days[-1], time(14, 30)))

    def test_legacy_scheduled_proxy_retries_only_after_lower_limit_and_respects_cap(self):
        self.write_bounds(([(2, 0.5)] * 3, [(3, 2)] * 3))
        open_at = datetime.combine(self.days[-1], time(14, 31))
        rows = self.case.market_rows(3.2, 3.1)
        rows.extend([(open_at, '600000.SH', 2.1), (open_at.replace(minute=32), '600000.SH', 2.2)])
        runner, _ = self.run_case(rows, split=False, cap=60)
        sim = runner.executor
        self.assertEqual([(x['ts'].time(), x['result']) for x in sim.substitution_attempts],
                         [(time(14, 30), 'NO_FILL'), (time(14, 31), 'PARTIAL'), (time(14, 32), 'FILLED')])
        self.assertEqual([(r['quantity'], r['price']) for r in sim.fills if r['purpose'] == 'MANAGER'],
                         [(60, D('2.1')), (40, D('2.2'))])
        self.assertFalse(sim.substitution_pricing)
