"""Full-runner regression checks for redemption sales at a minute-close lower limit."""
import csv
import json
import unittest
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal

import test_full_flow as fixtures
import test_market_split_substitution as market
from etf_arb.rules import SecurityTradingRule


class RedemptionLimitDownTests(unittest.TestCase):
    setUp = market.MarketSplitSubstitutionTests.setUp
    run_model = market.MarketSplitSubstitutionTests.run_model

    def bounds(self, rows):
        path = self.fixture.root / 'daily_limits.csv'
        with path.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['date', 'symbol', 'upper_limit', 'lower_limit', 'no_limit'])
            writer.writerows(rows)
        return replace(self.config, daily_price_limits_path=path)

    def test_real_holdings_and_cross_market_retry_until_their_own_unlock(self):
        first_day, next_day, _ = fixtures.DAYS
        config = self.bounds([(first_day, '000001.SZ', '1.1', '.90', 0),
                              (first_day, '600000.SH', '2.2', '1.80', 0),
                              (next_day, '000001.SZ', '1.05', '.85', 0)])
        unlock = self.entry + timedelta(minutes=2)
        rows = []
        for at, symbol, price in self.fixture.market_rows(3.2, 3.1):
            if symbol == '000001.SZ' and at.date() == first_day and at >= self.entry:
                price = .90
            if symbol == '600000.SH' and self.entry <= at < unlock:
                price = 1.80
            rows.append((at, symbol, price))
        runner, summary = self.run_model(rows, config)
        sim = runner.executor
        sells = {r['symbol']: r for r in sim.fills if r['side'] == 'SELL'}
        self.assertEqual(sells['000001.SZ']['ts'], datetime.combine(next_day, time(9, 30)))
        self.assertEqual(sells['600000.SH']['ts'], unlock)
        self.assertEqual(sim.daily[0]['holdings_value'], Decimal('90'))
        self.assertIsNone(sim.trades[0].redemption_prices_locked_at)
        self.assertEqual(sim.trades[0].delayed_redemption,
                         {'000001.SZ': 'limit_down:provided', '600000.SH': 'limit_down:provided'})
        self.assertFalse(sim.substitution_pricing)
        self.assertEqual(summary['settled_count'], 1)
        report = json.loads((config.output_dir / 'limit_exposure.json').read_text(encoding='utf-8'))
        self.assertEqual({r['symbol'] for r in report['rows']}, {'000001.SZ', '600000.SH'})
        self.assertEqual(len([r for r in sim.fee_rows if r['side'] == 'SELL']), 2)

    def test_redemption_applies_during_suspension_and_only_real_stock_fills_pay_fees(self):
        day, reopened, deadline = fixtures.DAYS
        feed = self.fixture.market / "stock_suspensions.csv"
        feed.write_text(f"symbol,start_date,end_date,source_url\n"
                        f"000001.SZ,{day},{day},https://source.example/sz\n"
                        f"600000.SH,{day},{deadline},https://source.example/sh\n")
        config = replace(self.config, clock_times=tuple(t for t in self.config.clock_times if t != time(9, 30)))
        rows = self.fixture.market_rows(3.2, 3.1)
        runner, summary = self.run_model(rows, config)
        sim = runner.executor
        self.assertEqual(summary["pending_count"], 1)
        local, = [r for r in sim.fills if r["symbol"] == "000001.SZ"]
        self.assertEqual((local["ts"], local["side"]), (datetime.combine(reopened, time(9, 30)), "SELL"))
        self.assertEqual(local["basis"], "first_tradable_close_after_suspension")
        self.assertFalse([r for r in sim.fills if r["symbol"] == "600000.SH"])
        self.assertFalse([r for r in sim.fee_rows if r["symbol"] == "600000.SH"])
        self.assertEqual(sim.daily[0]["holdings_value"], Decimal("100"))
        self.assertFalse(sim.substitution_pricing)
        self.assertIsNone(sim.trades[0].legs[1].final)
        self.assertIsNone(sim.trades[0].legs[1].paid_at)

    def test_resumed_proxy_waits_through_unknown_and_one_sided_limit_prices(self):
        day, resumed, _ = fixtures.DAYS
        feed = self.fixture.market / "stock_suspensions.csv"
        feed.write_text(f"symbol,start_date,end_date,source_url\n"
                        f"600000.SH,{day},{day},https://source.example/sh\n")
        config = self.bounds([(resumed, '600000.SH', '2.20', '1.80', 0)])
        rows = [(at, code, {time(9, 30): 2.30, time(9, 31): 2.20}.get(at.time(), 2)
                 if code == "600000.SH" and at.date() == resumed else price)
                for at, code, price in self.fixture.market_rows()]
        runner, _ = self.run_model(rows, config)
        fill, = [row for row in runner.executor.fills if row["symbol"] == "600000.SH"]
        self.assertEqual((fill["side"], fill["ts"]), ("BUY", datetime.combine(resumed, time(9, 32))))

    def test_existing_dated_suspension_rule_also_retries_on_first_stock_minute(self):
        day, resumed, end = fixtures.DAYS
        self.fixture.rules.add(SecurityTradingRule("000001.SZ", day, day, False, False, is_suspended=True))
        self.fixture.rules.add(SecurityTradingRule("000001.SZ", resumed, end, True, True))
        config = replace(self.config, clock_times=tuple(t for t in self.config.clock_times if t != time(9, 30)))
        runner, _ = self.run_model(self.fixture.market_rows(3.2, 3.1), config)
        fill, = [row for row in runner.executor.fills if row["symbol"] == "000001.SZ"]
        self.assertEqual((fill["ts"], fill["basis"]),
                         (datetime.combine(resumed, time(9, 30)), "first_tradable_close_after_suspension"))

    def test_unknown_after_block_does_not_release_and_deadline_only_values_proxy(self):
        day1, day2, day3 = fixtures.DAYS
        bounds = [(day, '600000.SH', '2.2', '1.8', 0) for day in fixtures.DAYS]
        bounds += [(day1, '000001.SZ', '1.1', '.9', 0),
                   (day2, '000001.SZ', '.88', '.80', 0),
                   (day3, '000001.SZ', '', '', 1)]
        config = self.bounds(bounds)
        rows = [(at, symbol, (.9 if symbol == '000001.SZ' else 1.8) if (
                    at >= self.entry and symbol in {'000001.SZ', '600000.SH'}) else price)
                for at, symbol, price in self.fixture.market_rows(3.2, 3.1)]
        # The remaining stock must still have limits even after rotating out of the PCF.
        for day in (day2, day3):
            path = self.fixture.pcf / '159338' / (day.strftime('%Y%m%d') + '.xml')
            root = ET.parse(path).getroot()
            components = root.find('Components')
            for component in list(components):
                if component.findtext('UnderlyingSecurityID') in {'000001', '600000'}:
                    components.remove(component)
            ET.ElementTree(root).write(path, encoding='utf-8')
        runner, _ = self.run_model(rows, config)
        sim = runner.executor
        sells = [f for f in sim.fills if f['side'] == 'SELL']
        self.assertEqual([(f['symbol'], f['ts']) for f in sells],
                         [('000001.SZ', datetime.combine(day3, time(9, 30)))])
        self.assertEqual([r['holdings_value'] for r in sim.daily[:2]], [Decimal('90')] * 2)
        self.assertFalse([r for r in sim.fee_rows if r['purpose'] == 'MANAGER'])
        self.assertEqual(len(sim.substitution_pricing), 1)
        mark, = sim.substitution_pricing
        self.assertEqual((mark['quantity'], mark['price'], mark['ts']),
                         (100, Decimal('1.8'), datetime.combine(day3, time(15))))
        self.assertEqual(mark['basis'], 'unfilled_quantity_at_deadline_close')
        self.assertEqual(sim.substitution_attempts[-1]['result'], 'NO_FILL')

    def test_upper_limit_close_allows_both_redemption_sells(self):
        day = self.entry.date()
        config = self.bounds([(day, '000001.SZ', '1.10', '.90', 0),
                              (day, '600000.SH', '2.20', '1.80', 0)])
        prices = {'000001.SZ': 1.10, '600000.SH': 2.20}
        rows = [(at, symbol, prices.get(symbol, price) if at == self.entry else price)
                for at, symbol, price in self.fixture.market_rows(3.2, 3.1)]
        runner, _ = self.run_model(rows, config)
        sells = {f['symbol']: (f['ts'], f['price']) for f in runner.executor.fills if f['side'] == 'SELL'}
        self.assertEqual(sells, {'000001.SZ': (self.entry, Decimal('1.10')),
                                 '600000.SH': (self.entry, Decimal('2.20'))})
        self.assertFalse(runner.executor.redemption_sell_blocks)

    def test_blocked_sales_follow_stock_minutes_between_scans_and_overnight(self):
        first, second, _ = fixtures.DAYS
        limits = {'000001.SZ': ('.90', '1.10'), '600000.SH': ('1.80', '2.20')}
        config = self.bounds([(day, symbol, upper, lower, 0) for day in fixtures.DAYS
                              for symbol, (lower, upper) in limits.items()])
        clocks = (time(11, 28), time(11, 29), time(13, 5), time(14, 30), time(15))
        execution = replace(config.simulation, trading_sessions=(('09:35', '11:29'), ('13:03', '14:57')))
        config = replace(config, clock_times=clocks, simulation=execution)
        blocked = datetime.combine(first, time(11, 29))
        for day, opening in [(first, time(13)), (second, time(9, 30))]:
            with self.subTest(opening=opening):
                local = datetime.combine(day, opening)
                proxy = local + timedelta(minutes=1)
                unlocks = {'000001.SZ': local, '600000.SH': proxy}
                moments = {datetime.combine(d, t) for d in fixtures.DAYS for t in clocks}
                noon = datetime.combine(first, time(12, 59))
                moments.update([noon, local, proxy])
                rows = []
                for at in sorted(moments):
                    rows.append((at, '159338.SZ', 3.2))
                    for symbol, unlock in unlocks.items():
                        down = float(limits[symbol][0])
                        price = down if blocked <= at < unlock and at != noon else down + .10
                        rows.append((at, symbol, price))
                runner, _ = self.run_model(rows, replace(config, output_dir=self.fixture.root / str(day)))
                sells = [(r['symbol'], r['ts']) for r in runner.executor.fills if r['side'] == 'SELL']
                self.assertCountEqual(sells, list(unlocks.items()))
                self.assertFalse(any(event.ts in (local, proxy) and event.kind == 'CLOCK' for event in runner._events))

    def test_missing_deadline_bar_keeps_only_its_own_proxy_unpriced(self):
        stocks = ('600000.SH', '600100.SH')
        config = self.bounds([(day, symbol, '2.20', '1.80', 0)
                              for day in fixtures.DAYS for symbol in stocks])
        for path in (self.fixture.pcf / '159338').glob('*.xml'):
            tree = ET.parse(path)
            components = tree.getroot().find('Components')
            extra = ET.SubElement(components, 'Component')
            for tag, value in [('UnderlyingSecurityID', '600100'), ('UnderlyingSecurityIDSource', '101'),
                               ('ComponentShare', '10'), ('SubstituteFlag', '1')]:
                ET.SubElement(extra, tag).text = value
            tree.write(path, encoding='utf-8')
        deadline = datetime.combine(fixtures.DAYS[-1], time(15))
        rows = []
        for at, symbol, price in self.fixture.market_rows(3.2, 3.1):
            if symbol == stocks[0]:
                price = 1.80 if at >= self.entry else price
                if at != deadline:
                    rows.append((at, stocks[1], price))
            rows.append((at, symbol, price))
        runner, _ = self.run_model(rows, replace(config, quality_check=False))
        sim = runner.executor
        self.assertEqual([(r['symbol'], r['quantity'], r['price']) for r in sim.substitution_pricing],
                         [(stocks[0], 100, Decimal('1.80'))])
        self.assertFalse(any(f['purpose'] == 'MANAGER' for f in sim.fee_rows))
        orders = {o.symbol: o for o in sim.orders}
        self.assertEqual(orders[stocks[0]].deadline_price, Decimal('1.80'))
        self.assertEqual(orders[stocks[1]].remaining, 10)
        cash = next(leg for leg in sim.trades[0].legs if leg.kind == 'cash_substitution')
        self.assertIsNone(cash.final)
        self.assertFalse(cash.paid)

    def test_legacy_scheduled_proxy_retries_a_limit_block_before_deadline(self):
        scheduled = self.scheduled
        config = self.bounds([(scheduled.date(), '600000.SH', '2.2', '1.80', 0)])
        config = replace(config, simulation=replace(config.simulation,
                         market_split_execution=False, max_shares_per_minute=60))
        rows = [(at, symbol, 1.80 if at == scheduled and symbol == '600000.SH' else price)
                for at, symbol, price in self.fixture.market_rows(3.2, 3.1)]
        rows += [(scheduled + timedelta(minutes=1), '600000.SH', 1.90),
                 (scheduled + timedelta(minutes=2), '600000.SH', 1.95)]
        runner, _ = self.run_model(rows, config)
        sim = runner.executor
        self.assertEqual([(f['ts'], f['quantity']) for f in sim.fills if f['purpose'] == 'MANAGER'],
                         [(scheduled + timedelta(minutes=1), 60), (scheduled + timedelta(minutes=2), 40)])
        self.assertFalse(sim.substitution_pricing)
        self.assertEqual([r['result'] for r in sim.substitution_attempts], ['NO_FILL', 'PARTIAL', 'FILLED'])
