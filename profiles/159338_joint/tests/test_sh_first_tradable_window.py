from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal as D
import unittest

import test_full_flow as fixtures
import test_market_split_substitution as base
from etf_arb.domain import Side
from etf_arb.rules import SecurityTradingRule


class SHFirstTradableWindowTests(unittest.TestCase):
    setUp = base.MarketSplitSubstitutionTests.setUp
    rows = base.MarketSplitSubstitutionTests.rows
    run_model = base.MarketSplitSubstitutionTests.run_model

    def restrict_to(self, first_day, clock, *, side=Side.CREATE):
        if first_day != fixtures.DAYS[0]:
            self.fixture.rules.add(SecurityTradingRule('600000.SH', fixtures.DAYS[0],
                fixtures.DAYS[fixtures.DAYS.index(first_day) - 1], False, False))
        self.fixture.rules.add(SecurityTradingRule('600000.SH', first_day, fixtures.DAYS[-1],
            True, True, trading_start=clock))

    def test_both_directions_find_first_minute_missing_from_runner_clocks(self):
        at = datetime.combine(fixtures.DAYS[0], time(10))
        self.restrict_to(fixtures.DAYS[0], time(10))
        for side, direction in ((Side.CREATE, 'BUY'), (Side.REDEEM, 'SELL')):
            rows = self.rows(side) + [(at, '600000.SH', 3)]
            config = replace(self.config, output_dir=self.fixture.root / side.value)
            runner, summary = self.run_model(rows, config)
            proxy = [f for f in runner.executor.fills if f['purpose'] == 'MANAGER']
            self.assertEqual([(f['ts'], f['side'], f['price']) for f in proxy], [(at, direction, D('3'))])
            self.assertEqual(proxy[0]['basis'], 'manager_first_tradable_close_proxy')
            self.assertEqual(runner.executor.substitution_attempts[-1]['timing'], 'sh_first_tradable_window')
            self.assertFalse(runner.executor.substitution_pricing)
            self.assertEqual(runner.executor.account.cash - config.starting_cash, D(summary['final_net_pnl']))
            self.assertEqual(len([r for r in runner.executor.fee_rows if r['purpose'] == 'MANAGER']), 1)
            if side is Side.REDEEM:
                self.assertIsNone(runner.executor.trades[0].redemption_prices_locked_at)

    def test_t_plus_two_first_tradable_minute_can_be_after_1430(self):
        at = datetime.combine(fixtures.DAYS[2], time(14, 59))
        self.restrict_to(fixtures.DAYS[2], time(14, 59))
        rows = self.rows() + [(at, '600000.SH', 4)]
        runner, _ = self.run_model(rows)
        proxy = next(f for f in runner.executor.fills if f['purpose'] == 'MANAGER')
        self.assertEqual((proxy['ts'], proxy['price']), (at, D('4')))
        self.assertFalse(runner.executor.substitution_pricing)
        self.assertIsNone(runner.executor.trades[0].legs[1].attempt_scheduled_at)

    def test_1500_first_tradable_fill_precedes_close_fallback_and_is_not_duplicated(self):
        at = datetime.combine(fixtures.DAYS[2], time(15))
        self.restrict_to(fixtures.DAYS[2], time(15))
        runner, _ = self.run_model()
        proxies = [f for f in runner.executor.fills if f['purpose'] == 'MANAGER']
        self.assertEqual([(f['ts'], f['quantity']) for f in proxies], [(at, 100)])
        self.assertFalse(runner.executor.substitution_pricing)
        self.assertEqual(len([r for r in runner.executor.substitution_attempts if r['ts'] == at]), 1)
        self.assertEqual(runner.executor.trades[0].legs[1].final, D('-677.18'))

    def test_never_tradable_uses_deadline_close_without_fills_or_manager_fees(self):
        self.fixture.rules.add(SecurityTradingRule(
            '600000.SH', fixtures.DAYS[0], fixtures.DAYS[-1], False, False))
        for side, expected in ((Side.CREATE, '-277'), (Side.REDEEM, '550')):
            at = datetime.combine(fixtures.DAYS[2], time(15))
            rows = [(ts, symbol, 5 if (ts, symbol) == (at, '600000.SH') else price)
                    for ts, symbol, price in self.rows(side)]
            config = replace(self.config, output_dir=self.fixture.root / side.value)
            runner, _ = self.run_model(rows, config)
            self.assertFalse(any(f['purpose'] == 'MANAGER' for f in runner.executor.fills))
            self.assertFalse(any(f['purpose'] == 'MANAGER' for f in runner.executor.fee_rows))
            valuation = runner.executor.substitution_pricing[0]
            self.assertEqual((valuation['ts'], valuation['quantity'], valuation['price']), (at, 100, D('5')))
            self.assertEqual(runner.executor.trades[0].legs[1].final, D(expected))
            self.assertEqual(runner.executor.substitution_attempts[-1]['ts'], at)

    def test_missing_deadline_close_stays_unpriced_and_never_uses_t_plus_three(self):
        self.fixture.rules.add(SecurityTradingRule(
            '600000.SH', fixtures.DAYS[0], fixtures.DAYS[-1], False, False))
        at = datetime.combine(fixtures.DAYS[2], time(15))
        rows = [r for r in self.rows() if (r[0], r[1]) != (at, '600000.SH')]
        rows += [(at + timedelta(minutes=1), '600000.SH', 0.1)]
        runner, summary = self.run_model(rows, replace(self.config, quality_check=False))
        self.assertEqual(runner.executor.trades[0].reason, 'missing_substitution_deadline_close')
        self.assertIsNone(runner.executor.trades[0].legs[1].final)
        self.assertIsNone(summary['final_net_pnl'])
        self.assertFalse(runner.executor.substitution_pricing)
        self.assertTrue(all(r['ts'] <= at for r in runner.executor.substitution_attempts))

    def test_lunch_and_overnight_quotes_are_not_fill_candidates(self):
        noon = datetime.combine(fixtures.DAYS[0], time(12))
        afternoon = datetime.combine(fixtures.DAYS[0], time(13))
        self.restrict_to(fixtures.DAYS[0], time(12))
        rows = self.rows() + [(noon, '600000.SH', 0.1), (afternoon, '600000.SH', 3),
                              (noon.replace(hour=16), '600000.SH', 0.1)]
        runner, _ = self.run_model(rows)
        self.assertEqual(next(f['ts'] for f in runner.executor.fills if f['purpose'] == 'MANAGER'), afternoon)
        self.assertFalse(any(r['ts'].time() == time(12) for r in runner.executor.substitution_attempts))

    def test_missing_quotes_are_not_forward_filled_and_first_available_price_wins(self):
        at = self.entry + timedelta(minutes=3)
        rows = [r for r in self.rows() if r[1] != '600000.SH' or r[0] < self.entry or r[0] > at]
        rows += [(at, '600000.SH', 3)]
        runner, _ = self.run_model(rows, replace(self.config, quality_check=False))
        proxies = [f for f in runner.executor.fills if f['purpose'] == 'MANAGER']
        self.assertEqual([(f['ts'], f['price']) for f in proxies], [(at, D('3'))])
        self.assertEqual([r['reason'] for r in runner.executor.substitution_attempts[:-1]], ['missing_exact_bar'] * 3)

    def test_run_horizon_before_t_plus_two_keeps_unfilled_stock_pending(self):
        self.fixture.rules.add(SecurityTradingRule(
            '600000.SH', fixtures.DAYS[0], fixtures.DAYS[-1], False, False))
        config = replace(self.config, trading_days=fixtures.DAYS[:1],
            settlement_end_date=fixtures.DAYS[0], simulation=replace(self.config.simulation,
                cash_substitution_lag=None, cash_difference_lag=None))
        runner, summary = self.run_model(config=config)
        self.assertIsNone(runner.executor.trades[0].legs[1].pricing_deadline)
        self.assertIsNone(runner.executor.trades[0].legs[1].final)
        self.assertEqual(summary['pending_count'], 1)
        self.assertIsNone(summary['final_net_pnl'])


if __name__ == '__main__':
    unittest.main()
