import csv
import json
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal as D
import unittest

import test_full_flow as fixtures
from etf_arb.costs import FeeRates
from etf_arb.domain import Side, TradeDecision
from etf_arb.full_flow import make_full_runner
from etf_arb.rules import SecurityTradingRule
from etf_arb.runner_config import RunnerConfig
from etf_arb.simulation_config import SimulationConfig
from test_missing_data import write_market


class MarketSplitSubstitutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.entry = datetime.combine(fixtures.DAYS[0], time(9, 31))
        self.scheduled = datetime.combine(fixtures.DAYS[2], time(14, 30))
        self.config = replace(self.fixture.config,
            simulation=replace(self.fixture.config.simulation,
                market_split_execution=True, max_trades_per_day=1, fees=FeeRates()))

    def rows(self, side=Side.CREATE, *, later_sh=9):
        initial, later = (4, 4.1) if side is Side.CREATE else (3.2, 3.1)
        return [(ts, symbol, (2.5 if ts == self.entry else later_sh if ts > self.entry else price)
                 if symbol == '600000.SH' else price)
                for ts, symbol, price in self.fixture.market_rows(initial, later)]

    def run_model(self, rows=None, config=None):
        return self.fixture.run_model(self.rows() if rows is None else rows, config or self.config)

    def require_sz_replacement(self):
        # Blocked only at signal time: already tradable at t+1, still must wait T+2.
        self.fixture.rules.add(SecurityTradingRule(
            '000001.SZ', fixtures.DAYS[0], fixtures.DAYS[-1], True, True, trading_start=time(9, 31)))

    def test_default_and_configuration_round_trip(self):
        self.assertTrue(SimulationConfig().market_split_execution)
        for invalid in (None, 1, 'true'):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                SimulationConfig(market_split_execution=invalid)
        with self.assertRaisesRegex(ValueError, 'requires assume_full_basket_fill'):
            SimulationConfig(assume_full_basket_fill=False)
        path = self.fixture.root / 'config.json'
        path.write_text(json.dumps(self.config.to_dict()), encoding='utf-8')
        self.assertTrue(RunnerConfig.from_json(path).simulation.market_split_execution)

    def test_sh_both_directions_execute_at_exact_t_plus_one_and_reconcile_fees(self):
        for side, direction, pnl in ((Side.CREATE, 'BUY', '-2.12'), (Side.REDEEM, 'SELL', '101.71')):
            with self.subTest(side=side):
                config = replace(self.config, output_dir=self.fixture.root / side.value)
                runner, summary = self.run_model(self.rows(side), config)
                trade = runner.executor.trades[0]
                proxies = [f for f in runner.executor.fills if f['purpose'] == 'MANAGER']
                self.assertEqual([(f['ts'], f['symbol'], f['side'], f['price']) for f in proxies],
                                 [(self.entry, '600000.SH', direction, D('2.5'))])
                self.assertEqual(proxies[0]['basis'], 'manager_next_minute_close_proxy')
                self.assertEqual(trade.applied_at, self.entry)
                if side is Side.REDEEM:
                    self.assertEqual({f['ts'] for f in runner.executor.fills}, {self.entry})
                    self.assertEqual(trade.redemption_prices_locked_at, self.entry)
                    self.assertEqual(summary['redemption_price_locked_count'], 1)
                self.assertFalse(runner.executor.substitution_pricing)
                self.assertEqual(D(summary['final_net_pnl']), D(pnl))
                self.assertEqual(runner.executor.account.cash - config.starting_cash, D(pnl))
                self.assertEqual(len(runner.resources.portfolio.to_rows()[0]), 6)
                self.assertEqual(runner.resources.portfolio.trades[0].opened_at.date(), fixtures.DAYS[0])
                self.assertFalse(any(row['symbol'] == '600000.SH' for row in runner.executor.account.rows))
                self.assertEqual(len([r for r in runner.executor.fee_rows if r['purpose'] == 'MANAGER']), 1)

    def test_later_sh_prices_do_not_change_fills_cashflows_or_profit(self):
        for side in (Side.CREATE, Side.REDEEM):
            baseline, summary = self.run_model(self.rows(side, later_sh=9),
                replace(self.config, output_dir=self.fixture.root / (side.value + '-base')))
            changed, changed_summary = self.run_model(self.rows(side, later_sh=0.1),
                replace(self.config, output_dir=self.fixture.root / (side.value + '-changed')))
            self.assertEqual(summary['final_net_pnl'], changed_summary['final_net_pnl'])
            self.assertEqual(baseline.executor.fills, changed.executor.fills)
            self.assertEqual(baseline.executor.account.rows, changed.executor.account.rows)

    def test_redemption_both_stock_markets_use_t_plus_one_even_if_later_prices_collapse(self):
        for future_price in (10, 0.1):
            rows = [(ts, symbol, (1.25 if ts == self.entry else future_price if ts > self.entry else price)
                     if symbol == '000001.SZ' else
                     (2.5 if ts == self.entry else future_price if ts > self.entry else price)
                     if symbol == '600000.SH' else price)
                    for ts, symbol, price in self.rows(Side.REDEEM)]
            config = replace(self.config, output_dir=self.fixture.root / str(future_price))
            runner, summary = self.run_model(rows, config)
            prices = {f['symbol']: f['price'] for f in runner.executor.fills}
            self.assertEqual(prices, {'159338.SZ': D('3.1'), '600000.SH': D('2.5'), '000001.SZ': D('1.25')})
            self.assertEqual({f['ts'] for f in runner.executor.fills}, {self.entry})
            self.assertEqual(D(summary['final_net_pnl']), D('126.70'))
            self.assertEqual(summary['delayed_redemption_count'], 0)

    def test_unsellable_sz_redemption_keeps_real_holdings_and_reports_unlocked_price(self):
        self.fixture.rules.add(SecurityTradingRule(
            '000001.SZ', fixtures.DAYS[0], fixtures.DAYS[0], True, False))
        self.fixture.rules.add(SecurityTradingRule(
            '000001.SZ', fixtures.DAYS[1], fixtures.DAYS[-1], True, True))
        runner, summary = self.run_model(self.rows(Side.REDEEM))
        trade = runner.executor.trades[0]
        self.assertIsNone(trade.redemption_prices_locked_at)
        self.assertEqual(trade.delayed_redemption, {'000001.SZ': 'security_rule_blocked'})
        self.assertEqual(summary['delayed_redemption_count'], 1)
        self.assertEqual(summary['redemption_price_locked_count'], 0)
        self.assertEqual(next(f['ts'] for f in runner.executor.fills if f['symbol'] == '000001.SZ'),
                         datetime.combine(fixtures.DAYS[1], time(9, 30)))
        self.assertEqual(runner.executor.daily[0]['holdings_value'], D('100'))

    def test_sz_recovered_at_t_plus_one_still_waits_for_t_plus_two(self):
        self.require_sz_replacement()
        rows = [(ts, symbol, 1.5 if symbol == '000001.SZ' and ts >= self.scheduled else price)
                for ts, symbol, price in self.rows()]
        # 14:30 is intentionally absent from the normal runner clock.
        config = replace(self.config, clock_times=tuple(t for t in fixtures.CLOCKS if t != time(14, 30)))
        runner, summary = self.run_model(rows, config)
        fills = {f['symbol']: f for f in runner.executor.fills}
        self.assertEqual(fills['600000.SH']['ts'], self.entry)
        self.assertEqual(fills['000001.SZ']['ts'], self.scheduled)
        self.assertEqual(fills['000001.SZ']['price'], D('1.5'))
        self.assertEqual(fills['000001.SZ']['purpose'], 'MANAGER')
        trade = runner.executor.trades[0]
        self.assertEqual(trade.plan.optional_creation_substitutions, {'000001.SZ': D('110')})
        self.assertEqual(trade.plan.substitution_deposit, D('363'))
        self.assertEqual(summary['settled_count'], 1)
        self.assertEqual(runner.executor.account.cash - config.starting_cash, D(summary['final_net_pnl']))
        attempts = runner.executor.substitution_attempts
        self.assertEqual([(r['symbol'], r['ts'], r['timing']) for r in attempts],
                         [('600000.SH', self.entry, 'sh_next_minute'),
                          ('000001.SZ', self.scheduled, 'scheduled_substitution')])

    def test_cap_only_limits_scheduled_replacement_without_duplicate_callbacks(self):
        self.require_sz_replacement()
        config = replace(self.config, simulation=replace(self.config.simulation, max_shares_per_minute=40))
        runner, _ = self.run_model(config=config)
        proxies = [f for f in runner.executor.fills if f['purpose'] == 'MANAGER']
        self.assertEqual([(f['symbol'], f['quantity']) for f in proxies],
                         [('600000.SH', 100), ('000001.SZ', 40)])
        self.assertEqual(len(runner.executor.substitution_attempts), 2)
        self.assertEqual(runner.executor.substitution_pricing[0]['symbol'], '000001.SZ')
        self.assertEqual(runner.executor.substitution_pricing[0]['quantity'], 60)
        self.assertEqual(sum(f['quantity'] for f in proxies if f['symbol'] == '000001.SZ'), 40)

    def test_missing_sz_scheduled_bar_uses_deadline_valuation_without_intraday_retry(self):
        self.require_sz_replacement()
        rows = [r for r in self.rows() if (r[0], r[1]) != (self.scheduled, '000001.SZ')]
        rows += [(datetime.combine(fixtures.DAYS[2], time(14, minute)), '000001.SZ', 5)
                 for minute in (29, 31)]
        config = replace(self.config, quality_check=False,
            clock_times=tuple(sorted(set(fixtures.CLOCKS + (time(14, 29), time(14, 31))))))
        runner, _ = self.run_model(rows, config)
        self.assertFalse(any(f['symbol'] == '000001.SZ' for f in runner.executor.fills))
        attempt = runner.executor.substitution_attempts[-1]
        self.assertEqual((attempt['ts'], attempt['reason']), (self.scheduled, 'missing_exact_bar'))
        self.assertEqual(runner.executor.substitution_pricing[0]['price'], D('1'))

    def test_sh_missing_t_plus_one_quote_retries_the_next_minute(self):
        rows = [r for r in self.rows() if (r[0], r[1]) != (self.entry, '600000.SH')]
        runner, _ = self.run_model(rows, replace(self.config, quality_check=False))
        attempts = runner.executor.substitution_attempts
        self.assertEqual([(r['ts'], r['result']) for r in attempts],
                         [(self.entry, 'NO_FILL'), (datetime.combine(fixtures.DAYS[0], time(9, 32)), 'FILLED')])
        self.assertEqual(attempts[0]['reason'], 'missing_exact_bar')
        proxy = next(f for f in runner.executor.fills if f['purpose'] == 'MANAGER')
        first_available = datetime.combine(fixtures.DAYS[0], time(9, 32))
        self.assertEqual((proxy['ts'], proxy['price']), (first_available, D('9')))
        order = next(o for o in runner.executor.orders if o.purpose == 'MANAGER')
        self.assertEqual((order.next_minute_attempted_at, order.last_sh_attempted_at),
                         (self.entry, first_available))
        self.assertIsNone(order.manager_attempted_at)

    def test_sh_trading_restrictions_apply_to_the_requested_direction(self):
        self.fixture.rules.add(SecurityTradingRule(
            '600000.SH', fixtures.DAYS[0], fixtures.DAYS[0], False, True))
        self.fixture.rules.add(SecurityTradingRule(
            '600000.SH', fixtures.DAYS[1], fixtures.DAYS[-1], True, True))
        for side, expected in ((Side.CREATE, datetime.combine(fixtures.DAYS[1], time(9, 30))),
                               (Side.REDEEM, self.entry)):
            runner, _ = self.run_model(self.rows(side),
                replace(self.config, output_dir=self.fixture.root / side.value))
            self.assertEqual(next(f for f in runner.executor.fills if f['purpose'] == 'MANAGER')['ts'], expected)

    def test_no_next_runner_minute_blocks_source_and_external_decision(self):
        config = replace(self.config, clock_times=(time(9, 30), time(9, 32), time(15)))
        write_market(self.fixture.market, self.rows())
        runner = make_full_runner(config, rules=self.fixture.rules)
        decision = TradeDecision('159338', datetime.combine(fixtures.DAYS[0], time(9, 30)),
                                 True, Side.CREATE, 1, D('1'), D('0'), D('1'))
        result = runner.run([decision])
        self.assertEqual(result.financial_summary['opened_count'], 0)
        self.assertFalse(runner.executor.fills)
        self.assertIn('no_exact_next_minute_for_market_split', {r['reason'] for r in runner.source.rows})
        self.assertIn('no_exact_next_minute_for_market_split', {r['reason'] for r in runner.executor.rejections})

    def test_lunch_is_not_a_next_minute(self):
        clocks = (time(11, 27), time(13), time(15))
        rows = [(datetime.combine(day, clock), symbol, price)
                for day in fixtures.DAYS for clock in clocks
                for symbol, price in (('159338.SZ', 4), ('000001.SZ', 1), ('600000.SH', 2))]
        config = replace(self.config, clock_times=clocks,
            simulation=replace(self.config.simulation, trading_sessions=(('09:35', '11:27'), ('13:00', '14:57'))))
        runner, summary = self.run_model(rows, config)
        self.assertEqual(summary['opened_count'], 0)
        self.assertEqual(runner.source.rows[0]['reason'], 'no_exact_next_minute_for_market_split')

    def test_locked_sh_prices_do_not_release_unpaid_cash(self):
        config = replace(self.config, simulation=replace(self.config.simulation,
            cash_substitution_lag=None, cash_difference_lag=None))
        runner, summary = self.run_model(config=config)
        self.assertIsNone(summary['final_net_pnl'])
        self.assertEqual(summary['pending_count'], 1)
        self.assertTrue(all(leg.final is not None and not leg.paid for leg in runner.executor.trades[0].legs))
        self.assertFalse(any(r['event'] in {'CASH_SETTLED', 'CASH_SUBSTITUTION_SETTLED'}
                             for r in runner.executor.account.rows))

    def test_standard_outputs_explain_market_split_timing(self):
        self.require_sz_replacement()
        _, summary = self.run_model()
        self.assertTrue(summary['market_split_execution'])
        with (self.config.output_dir / 'substitution_attempts.csv').open(encoding='utf-8-sig', newline='') as stream:
            attempts = list(csv.DictReader(stream))
        self.assertEqual([r['timing'] for r in attempts], ['sh_next_minute', 'scheduled_substitution'])
        assumptions = json.loads((self.config.output_dir / 'assumptions.json').read_text(encoding='utf-8'))
        self.assertTrue(assumptions['market_split_execution'])
        self.assertIn('SZ optional creation replacements always wait', assumptions['manager_execution'])
        html = (self.config.output_dir / 'report/report.html').read_text(encoding='utf-8')
        self.assertIn('沪深代买卖时点分离', html)
        ledger = (self.config.output_dir / 'report/ledger.html').read_text(encoding='utf-8')
        self.assertIn('sh_next_minute', ledger)
        self.assertIn('scheduled_substitution', ledger)


if __name__ == '__main__':
    unittest.main()
