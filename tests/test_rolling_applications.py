import csv
import json
import unittest
from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal as D

import test_full_flow as fixtures
from etf_arb.costs import FeeRates
from etf_arb.domain import Side
from etf_arb.rules import SecurityTradingRule


CLOCKS = tuple(time.fromisoformat(clock) for clock in (
    '09:35', '09:36', '09:37', '10:34', '10:35', '10:36', '10:37', '10:38',
    '11:26', '11:27', '13:00', '13:01', '13:02', '14:00', '14:01', '14:02',
    '14:03', '14:57', '15:00'))


class RollingApplicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = replace(self.fixture.config, clock_times=CLOCKS,
            simulation=replace(self.fixture.config.simulation, fees=FeeRates(),
                blocked_trade_minutes=('09:30',),
                trading_sessions=(('09:35', '11:27'), ('13:00', '14:57')),
                cash_substitution_lag=None, cash_difference_lag=None))

    def run_model(self, config=None, etf_price=None):
        config = config or self.config
        etf_price = etf_price or (lambda clock: 4.1)
        # A scheduled T+2 manager attempt need not be a regular runner clock.
        clocks = sorted(set(config.clock_times + (time(14, 30),)))
        rows = [(datetime.combine(day, clock), symbol,
                 etf_price(clock) if symbol == '159338.SZ' else price)
                for day in fixtures.DAYS for clock in clocks
                for symbol, price in (('159338.SZ', 0), ('000001.SZ', 1), ('600000.SH', 2))]
        return self.fixture.run_model(rows, config)

    def test_four_same_day_cycles_open_while_all_previous_cash_is_unpaid(self):
        runner, summary = self.run_model()
        trades = runner.executor.trades
        self.assertEqual([t.decision.ts.time() for t in trades],
                         [time(9, 35), time(10, 36), time(13), time(14, 1)])
        self.assertEqual([t.applied_at.time() for t in trades],
                         [time(9, 36), time(10, 37), time(13, 1), time(14, 2)])
        for previous, current in zip(trades, trades[1:]):
            self.assertGreaterEqual(current.decision.ts - previous.applied_at, timedelta(hours=1))
        self.assertEqual(summary['frequency_anchor'], 'application_submitted_at')
        self.assertEqual(summary['peak_open_cycles'], 4)
        self.assertEqual(summary['pending_count'], 4)
        self.assertEqual(summary['settled_count'], 0)
        self.assertEqual(summary['priced_unpaid_cash_legs'], 8)
        self.assertIsNone(summary['final_net_pnl'])
        self.assertEqual(D(summary['marked_account_pnl']), D('191.60'))
        self.assertFalse(any(row['event'] in {'CASH_SETTLED', 'CASH_SUBSTITUTION_SETTLED'}
                             for row in runner.executor.account.rows))
        self.assertTrue(all(not runner.executor.account.holdings(t.trade_id) for t in trades))
        self.assertEqual(runner.executor.started_by_day, {fixtures.DAYS[0]: 4})

    def test_overlapping_cycles_reconcile_fees_cash_and_independent_settlement(self):
        config = replace(self.config, simulation=replace(self.config.simulation,
            cash_substitution_lag=2, cash_difference_lag=1))
        runner, summary = self.run_model(config)
        self.assertEqual(summary['opened_count'], 4)
        self.assertEqual(summary['settled_count'], 4)
        self.assertEqual(summary['pending_count'], 0)
        self.assertEqual(summary['peak_open_cycles'], 4)
        self.assertEqual(D(summary['final_net_pnl']), D('191.60'))
        self.assertEqual(D(summary['ending_cash']), D('10191.60'))
        self.assertEqual(D(summary['fee']), D('0.40'))
        self.assertEqual(D(summary['investor_fees']), D('0.24'))
        self.assertEqual(D(summary['manager_fees']), D('0.16'))
        for trade in runner.executor.trades:
            self.assertEqual(trade.state, 'CLOSED')
            self.assertEqual(runner.executor.account.trade_cash[trade.trade_id], D('47.90'))
            self.assertTrue(all(leg.paid for leg in trade.legs))
            cash_events = [row['event'] for row in runner.executor.account.rows
                           if row['trade_id'] == trade.trade_id]
            self.assertEqual(cash_events.count('CASH_SETTLED'), 1)
            self.assertEqual(cash_events.count('CASH_SUBSTITUTION_SETTLED'), 1)
            self.assertFalse(runner.executor.account.holdings(trade.trade_id))
        manager_fills = [f for f in runner.executor.fills if f['purpose'] == 'MANAGER']
        self.assertEqual(len(manager_fills), 4)
        self.assertEqual({f['ts'] for f in manager_fills},
                         {datetime.combine(fixtures.DAYS[-1], time(14, 30))})
        self.assertEqual(len(runner.resources.portfolio.to_rows()[0]), 6)
        with (config.output_dir / 'trades.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]['applied_at'], '2025-01-02 09:36:00')
        self.assertEqual(rows[0]['next_signal_not_before'], '2025-01-02 10:36:00')
        assumptions = json.loads((config.output_dir / 'assumptions.json').read_text(encoding='utf-8'))
        self.assertIn('Overlapping cycles are allowed', assumptions['scheduling'])
        html = (config.output_dir / 'report/report.html').read_text(encoding='utf-8')
        self.assertIn('满 60 分钟后', html)
        self.assertIn('允许多笔未结清套利并行', html)
        self.assertIn('未结清套利数量峰值：4 笔', html)
        ledger = (config.output_dir / 'report/ledger.html').read_text(encoding='utf-8')
        self.assertIn('applied_at', ledger)
        self.assertIn('next_signal_not_before', ledger)

    def test_opposite_directions_overlap_and_keep_cashflows_separate(self):
        clocks = tuple(time.fromisoformat(clock) for clock in (
            '09:35', '09:36', '09:37', '10:35', '10:36', '10:37', '10:38', '14:30', '15:00'))
        config = replace(self.config, clock_times=clocks,
            simulation=replace(self.config.simulation, cash_substitution_lag=2, cash_difference_lag=1))
        runner, summary = self.run_model(config, lambda clock: 4.1 if clock < time(10, 36) else 3.1)
        self.assertEqual([t.decision.side for t in runner.executor.trades], [Side.CREATE, Side.REDEEM])
        self.assertEqual(summary['peak_open_cycles'], 2)
        self.assertEqual(summary['settled_count'], 2)
        self.assertEqual(D(summary['final_net_pnl']), D('99.66'))
        self.assertEqual(runner.executor.account.trade_cash, {'T000001': D('47.90'), 'T000002': D('51.76')})
        manager = [f for f in runner.executor.fills if f['purpose'] == 'MANAGER']
        self.assertEqual([(f['trade_id'], f['side']) for f in manager],
                         [('T000001', 'BUY'), ('T000002', 'SELL')])
        self.assertFalse(any(runner.executor.account.positions.values()))

    def test_delayed_submission_starts_hour_at_application_instead_of_signal(self):
        clocks = tuple(time.fromisoformat(clock) for clock in (
            '09:35', '10:00', '10:01', '10:35', '10:59', '11:00', '11:01', '11:02', '14:30', '15:00'))
        runner, summary = self.run_model(replace(self.config, clock_times=clocks))
        self.assertEqual(summary['opened_count'], 2)
        self.assertEqual([t.decision.ts.time() for t in runner.executor.trades], [time(9, 35), time(11)])
        self.assertEqual([t.applied_at.time() for t in runner.executor.trades], [time(10), time(11, 1)])
        reasons = {row['ts'].time(): row['reason'] for row in runner.source.rows
                   if row['ts'].date() == fixtures.DAYS[0]}
        self.assertEqual(reasons[time(10, 35)], 'application_interval_or_pending_start')
        self.assertEqual(reasons[time(10, 59)], 'application_interval_or_pending_start')
        self.assertEqual(reasons[time(11)], 'threshold_crossed')

    def test_lunch_counts_as_elapsed_time_and_sessions_still_apply(self):
        clocks = tuple(time.fromisoformat(clock) for clock in (
            '09:30', '11:26', '11:27', '12:27', '13:00', '13:01', '13:02', '14:30', '15:00'))
        runner, summary = self.run_model(replace(self.config, clock_times=clocks))
        self.assertEqual(summary['opened_count'], 2)
        self.assertEqual([t.decision.ts.time() for t in runner.executor.trades], [time(11, 26), time(13)])
        self.assertEqual(runner.executor.trades[0].applied_at.time(), time(11, 27))
        reasons = {row['ts'].time(): row['reason'] for row in runner.source.rows
                   if row['ts'].date() == fixtures.DAYS[0]}
        self.assertEqual(reasons[time(9, 30)], 'blocked_trade_minute')
        self.assertEqual(reasons[time(12, 27)], 'outside_trading_sessions')
        self.assertTrue(all(runner.executor.trading_times.allows(f['ts'].time())
                            for f in runner.executor.fills if f['purpose'] != 'MANAGER'))

    def test_unpaid_receivables_cannot_fund_the_next_eligible_signal(self):
        runner, summary = self.run_model(replace(self.config, starting_cash=D('400')), lambda clock: 3.1)
        self.assertEqual(summary['opened_count'], 1)
        self.assertEqual(summary['pending_count'], 1)
        self.assertEqual(D(summary['ending_cash']), D('179.90'))
        self.assertEqual(D(summary['ending_equity']), D('451.76'))
        self.assertTrue(any(row['ts'].time() == time(10, 36) and row['reason'] == 'insufficient_cash'
                            for row in runner.executor.rejections))
        self.assertTrue(all(row['cash_balance'] >= 0 for row in runner.executor.account.rows))
        self.assertFalse(any(leg.paid for leg in runner.executor.trades[0].legs))

    def test_existing_security_holdings_do_not_block_later_hourly_applications(self):
        self.fixture.rules.add(SecurityTradingRule('159338.SZ', fixtures.DAYS[0], fixtures.DAYS[0], True, False))
        self.fixture.rules.add(SecurityTradingRule('159338.SZ', fixtures.DAYS[1], fixtures.DAYS[-1], True, True))
        runner, summary = self.run_model()
        self.assertEqual(summary['opened_count'], 4)
        self.assertEqual(runner.executor.daily[0]['holdings_value'], D('1640'))
        exits = [f for f in runner.executor.fills if f['purpose'] == 'EXIT']
        self.assertEqual(len(exits), 4)
        self.assertEqual({f['ts'].date() for f in exits}, {fixtures.DAYS[1]})
        self.assertEqual({f['trade_id'] for f in exits}, {t.trade_id for t in runner.executor.trades})
        self.assertTrue(all(f['quantity'] == 100 for f in exits))
        self.assertFalse(any(runner.executor.account.positions.values()))


if __name__ == '__main__':
    unittest.main()
