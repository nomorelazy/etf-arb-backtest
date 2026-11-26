import csv
import json
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal
import unittest

import test_full_flow as fixtures
from etf_arb.rules import SecurityTradingRule


class SecurityClockSettlementTests(unittest.TestCase):
    def setUp(self):
        self.case = fixtures.FullFlowTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def prepare(self, count, halts, *, market_split):
        days = [fixtures.DAYS[0]]
        while len(days) < count:
            day = days[-1] + timedelta(days=1)
            while day.weekday() >= 5:
                day += timedelta(days=1)
            days.append(day)
        days = tuple(days)
        pcf = self.case.pcf / '159338'
        template = (pcf / '20250106.xml').read_text(encoding='utf-8')
        for i in range(3, len(days)):
            (pcf / f'{days[i]:%Y%m%d}.xml').write_text(
                template.replace('20250106', days[i].strftime('%Y%m%d'))
                        .replace('20250103', days[i-1].strftime('%Y%m%d')), encoding='utf-8')
        path = self.case.root / 'halts.csv'
        with path.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['symbol', 'start_date', 'end_date', 'source_url'])
            for symbol, first, last in halts:
                writer.writerow([symbol, days[first], days[last], 'https://example.org/notice'])
        settings = replace(self.case.config.simulation, market_split_execution=market_split,
                           cash_substitution_lag=3, max_trades_per_day=1)
        config = replace(self.case.config, trading_days=days, settlement_end_date=days[-1],
                         simulation=settings, stock_suspensions_path=path, quality_check=False)
        rows = [(datetime.combine(day, clock), symbol, price)
                for day in days for clock in fixtures.CLOCKS
                for symbol, price in [('159338.SZ', 4.0 if clock == time(9,30) else 4.1),
                                      ('000001.SZ', 1), ('600000.SH', 2)]]
        return days, config, rows

    def test_independent_order_deadlines_and_last_pricing_research_payment(self):
        days, config, rows = self.prepare(5, [('000001.SZ', 0, 1)], market_split=True)
        bounds = self.case.root / 'bounds.csv'
        bounds.write_text('date,symbol,upper_limit,lower_limit\n2025-01-02,600000.SH,2,1.7\n', encoding='utf-8')
        config = replace(config, daily_price_limits_path=bounds)
        # SH cannot buy until day 4, after its own (earlier) deadline.
        self.case.rules.add(SecurityTradingRule('600000.SH', days[0], days[2], False, True))
        self.case.rules.add(SecurityTradingRule('600000.SH', days[3], days[-1], True, True))
        rows = [(at, symbol, 3 if symbol == '600000.SH' and at.date() >= days[3] else price)
                for at, symbol, price in rows]
        (self.case.market / 'report_prior_closes.csv').write_text(
            'previous_date,symbol,close,source_url\n2024-12-31,000001.SZ,1,https://example.org/close\n',
            encoding='utf-8')
        runner, summary = self.case.run_model(rows, config)
        executor = runner.executor
        trade, = executor.trades
        orders = {order.symbol: order for order in executor._orders(trade, 'MANAGER')}
        sh, sz = orders['600000.SH'], orders['000001.SZ']
        self.assertEqual(sh.pricing_deadline, days[2])
        self.assertEqual((sh.filled, sh.deadline_valuation_price, sh.fee), (0, Decimal(2), 0))
        impact = json.loads((config.output_dir / 'stock_limit_impact.json').read_text(encoding='utf-8'))
        stock = next(row for row in impact['rows'] if row['symbol'] == sh.symbol)
        self.assertEqual(stock['pricing_deadline'], days[2].isoformat())
        self.assertEqual((sz.pricing_deadline, sz.attempt_scheduled_at),
                         (days[3], datetime.combine(days[3], time(14,30))))
        self.assertEqual((sz.filled, sz.manager_attempted_at), (100, sz.attempt_scheduled_at))
        pricing, = executor.substitution_pricing
        self.assertEqual((pricing['symbol'], pricing['source_ts']), (sh.symbol, datetime.combine(days[2], time(15))))
        self.assertEqual([f['symbol'] for f in executor.fills if f['purpose'] == 'MANAGER'], [sz.symbol])
        sub = trade.legs[1]
        self.assertEqual((sub.priced_at, sub.due, sub.paid_at.date()), (datetime.combine(days[3], time(15)), days[4], days[4]))
        self.assertEqual(summary['settled_count'], 1)

    def test_early_execution_keeps_its_fill_but_cannot_confirm_before_required_day_end(self):
        days, config, rows = self.prepare(2, [], market_split=True)
        runner, summary = self.case.run_model(rows, config)
        trade, = runner.executor.trades
        order, = runner.executor._orders(trade, 'MANAGER')
        self.assertEqual((order.filled, order.value), (100, 200))
        self.assertEqual(order.last_cross_market_attempted_at.date(), days[0])
        self.assertIsNone(trade.legs[1].known_on)
        self.assertIsNone(trade.legs[1].final)
        self.assertIsNone(trade.legs[1].paid_at)
        self.assertEqual(summary['pending_count'], 1)

    def test_day_twenty_uses_last_actual_close_and_no_valuation_trading_fees(self):
        days, config, rows = self.prepare(21, [('600000.SH', 1, 20)], market_split=False)
        rows = [(at, symbol, 88 if symbol == '600000.SH' and at.date() > days[0] else price)
                for at, symbol, price in rows]
        runner, summary = self.case.run_model(rows, config)
        executor = runner.executor
        trade, = executor.trades
        order, = executor._orders(trade, 'MANAGER')
        pricing, = executor.substitution_pricing
        self.assertEqual((order.pricing_deadline, order.deadline_basis), (days[19], 'exchange_20_day_cap'))
        self.assertIsNone(order.attempt_scheduled_at)
        self.assertEqual(trade.legs[1].notice_due_on, days[20])
        self.assertIsNone(trade.legs[1].latest_payment_on)
        self.assertEqual((pricing['price'], pricing['quantity'], pricing['source_ts'], pricing['ts']),
                         (2, 100, datetime.combine(days[0], time(15)), datetime.combine(days[19], time(15))))
        self.assertFalse(any(f['purpose'] == 'MANAGER' for f in executor.fills + executor.fee_rows))
        self.assertEqual((trade.legs[1].due, trade.legs[1].paid_at.date()), (days[20], days[20]))
        self.assertEqual(summary['settled_count'], 1)

    def test_cap_without_last_actual_close_cannot_use_frozen_quotes_or_later_resumed_fill(self):
        days, config, rows = self.prepare(21, [('600000.SH', 1, 19)], market_split=False)
        rows = [row for row in rows if (row[0], row[1]) != (datetime.combine(days[0], time(15)), '600000.SH')]
        runner, summary = self.case.run_model(rows, config)
        trade, = runner.executor.trades
        order, = runner.executor._orders(trade, 'MANAGER')
        self.assertEqual((order.pricing_deadline, order.remaining), (days[19], 100))
        self.assertFalse(runner.executor.substitution_pricing)
        self.assertFalse(any(row['purpose'] == 'MANAGER' for row in runner.executor.fills))
        self.assertIsNone(trade.legs[1].final)
        self.assertIsNone(trade.legs[1].paid_at)
        self.assertEqual(summary['pending_count'], 1)
