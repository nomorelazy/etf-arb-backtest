import csv
import json
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal as D
import unittest
import xml.etree.ElementTree as ET

import test_full_flow as fixtures
from etf_arb.domain import Side
from etf_arb.costs import FeeRates
from etf_arb.full_flow import make_full_runner
from etf_arb.rules import SecurityTradingRule
from etf_arb.runner_config import RunnerConfig
from etf_arb.simulation_config import SimulationConfig
from test_missing_data import write_market


class EstimatedCashReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = replace(self.fixture.config, simulation=replace(self.fixture.config.simulation,
            market_split_execution=True, estimated_cash_interim_settlement=True,
            redemption_sales_at_next_minute=True, cash_difference_lag=None, max_trades_per_day=1))
        self.entry = datetime.combine(fixtures.DAYS[0], time(9, 31))
        self.adjustment_at = datetime.combine(fixtures.DAYS[1], time(9, 30))

    def pcf_field(self, index, field, value):
        path = self.fixture.pcf / '159338' / f'{fixtures.DAYS[index]:%Y%m%d}.xml'
        tree = ET.parse(path)
        element = tree.find(field)
        if value is None:
            tree.getroot().remove(element)
        else:
            element.text = str(value)
        tree.write(path, encoding='utf-8')

    def rows(self, side):
        return self.fixture.market_rows(4, 4.1) if side is Side.CREATE else self.fixture.market_rows(3.2, 3.1)

    def test_signed_estimate_and_next_day_difference_reconcile_both_directions(self):
        cases = ((10, 12), (10, 8), (-10, -12), (-10, 12), (0, 12), (10, 10))
        for side in (Side.CREATE, Side.REDEEM):
            for estimate, actual in cases:
                for units in (1, 2):
                    with self.subTest(side=side, estimate=estimate, actual=actual, units=units):
                        self.pcf_field(0, 'EstimateCashComponent', estimate)
                        self.pcf_field(1, 'CashComponent', actual)
                        config = replace(self.config, output_dir=self.fixture.root / f'{side.value}-{estimate}-{actual}-{units}',
                            simulation=replace(self.config.simulation, quantity_units=units))
                        runner, summary = self.fixture.run_model(self.rows(side), config)
                        trade = runner.executor.trades[0]
                        leg = trade.legs[0]
                        sign = D(-1 if side is Side.CREATE else 1)
                        e, a = D(estimate * units), D(actual * units)
                        applied = next(r for r in runner.executor.account.rows
                                       if r['event'] in {'CREATE_SUBMITTED', 'REDEEM_SUBMITTED'})
                        fixed = trade.plan.substitution_deposit + trade.plan.domestic_fixed if side is Side.CREATE else D(0)
                        self.assertEqual(applied['cash_delta'] + fixed, sign * e)
                        self.assertEqual(leg.initial_cash_flow, sign * e)
                        self.assertEqual(leg.estimated_component, e)
                        self.assertEqual(leg.actual_component, a)
                        self.assertEqual(leg.component_difference, a - e)
                        self.assertEqual(leg.final, sign * (a - e))
                        self.assertEqual(leg.initial_cash_flow + leg.final, sign * a)
                        self.assertEqual(leg.paid_at, self.adjustment_at)
                        self.assertEqual(leg.payment_basis, 'next_day_adjustment_research')
                        adjustments = [r for r in runner.executor.account.rows if r['event'] == 'CASH_SETTLED']
                        self.assertEqual([(r['ts'], r['cash_delta']) for r in adjustments],
                                         [(self.adjustment_at, sign * (a - e))])
                        self.assertEqual(runner.executor.account.cash - config.starting_cash, D(summary['final_net_pnl']))
                        self.assertEqual(len(runner.resources.portfolio.to_rows()[0]), 6)
                        self.assertEqual(runner.resources.portfolio.trades[0].opened_at.date(), fixtures.DAYS[0])

    def test_new_defaults_and_settings_round_trip(self):
        settings = SimulationConfig()
        self.assertTrue(settings.estimated_cash_interim_settlement)
        self.assertTrue(settings.redemption_sales_at_next_minute)
        for name in ('estimated_cash_interim_settlement', 'redemption_sales_at_next_minute'):
            for value in (None, 'true', 1):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    SimulationConfig(**{name: value})
        path = self.fixture.root / 'config.json'
        path.write_text(json.dumps(self.config.to_dict()), encoding='utf-8')
        loaded = RunnerConfig.from_json(path).simulation
        self.assertTrue(loaded.estimated_cash_interim_settlement)
        self.assertTrue(loaded.redemption_sales_at_next_minute)

    def test_missing_t_plus_one_amount_does_not_use_later_pcf(self):
        self.pcf_field(1, 'CashComponent', None)
        self.pcf_field(2, 'PreTradingDay', f'{fixtures.DAYS[0]:%Y%m%d}')
        self.pcf_field(2, 'CashComponent', 999)
        runner, summary = self.fixture.run_model(self.rows(Side.CREATE), self.config)
        leg = runner.executor.trades[0].legs[0]
        self.assertIsNone(leg.final)
        self.assertFalse(leg.paid)
        self.assertIsNone(summary['final_net_pnl'])
        self.assertEqual(summary['pending_count'], 1)

    def test_wrong_pretradingday_cannot_reconcile_application(self):
        self.pcf_field(1, 'PreTradingDay', '20241231')
        runner, summary = self.fixture.run_model(self.rows(Side.REDEEM), self.config)
        self.assertIsNone(runner.executor.trades[0].legs[0].final)
        self.assertIsNone(summary['final_net_pnl'])

    def test_actual_amount_is_not_used_before_next_day(self):
        snapshots = []
        write_market(self.fixture.market, self.rows(Side.CREATE))
        runner = make_full_runner(self.config, rules=self.fixture.rules)
        original = runner.executor.on_day_close
        def observe(context):
            if context.now.date() == fixtures.DAYS[0]:
                leg = runner.executor.trades[0].legs[0]
                snapshots.append((leg.final, leg.paid, leg.outstanding))
            original(context)
        runner.hooks = replace(runner.hooks, on_day_close=observe)
        runner.run()
        self.assertEqual(snapshots, [(None, False, D(0))])

    def test_explicit_payment_record_overrides_next_day_research_time(self):
        paid_at = datetime.combine(fixtures.DAYS[1], time(9))
        path = self.fixture.root / 'payments.json'
        path.write_text(json.dumps([dict(trade_id='T000001', kind='cash_difference',
                                         paid_at=paid_at.isoformat())]), encoding='utf-8')
        config = replace(self.config, cash_payment_dates_path=path)
        runner, _ = self.fixture.run_model(self.rows(Side.REDEEM), config)
        leg = runner.executor.trades[0].legs[0]
        self.assertEqual(leg.paid_at, paid_at)
        self.assertEqual(leg.payment_basis, 'supplied_payment_timestamp')
        self.assertEqual(len([r for r in runner.executor.account.rows if r['event'] == 'CASH_SETTLED']), 1)

    def test_no_funding_from_redemption_estimate_before_etf_purchase(self):
        self.pcf_field(0, 'EstimateCashComponent', 10)
        runner, summary = self.fixture.run_model(self.rows(Side.REDEEM),
                                                replace(self.config, starting_cash=D(300)))
        self.assertEqual(summary['opened_count'], 0)
        self.assertFalse(runner.executor.fills)
        self.assertEqual(runner.executor.account.cash, D(300))

    def test_negative_redemption_estimate_is_reserved_before_entry(self):
        self.pcf_field(0, 'EstimateCashComponent', -10)
        runner, summary = self.fixture.run_model(self.rows(Side.REDEEM),
                                                replace(self.config, starting_cash=D(315)))
        self.assertEqual(summary['opened_count'], 0)
        self.assertFalse(runner.executor.fills)

    def test_negative_creation_estimate_offsets_application_cash_once(self):
        self.pcf_field(0, 'EstimateCashComponent', -10)
        self.pcf_field(1, 'CashComponent', -12)
        runner, summary = self.fixture.run_model(self.rows(Side.CREATE),
            replace(self.config, starting_cash=D(363)))
        self.assertEqual(summary['opened_count'], 1)
        self.assertTrue(all(r['cash_balance'] >= 0 for r in runner.executor.account.rows))
        self.assertEqual(runner.executor.trades[0].legs[0].final, D(2))

    def test_redemption_sales_all_use_t_plus_one_and_ignore_later_prices(self):
        for later in (0.1, 99):
            rows = [(ts, code, later if code in {'600000.SH', '000001.SZ'} and ts > self.entry else price)
                    for ts, code, price in self.rows(Side.REDEEM)]
            config = replace(self.config, output_dir=self.fixture.root / str(later))
            runner, summary = self.fixture.run_model(rows, config)
            self.assertEqual({f['ts'] for f in runner.executor.fills}, {self.entry})
            self.assertEqual(D(summary['final_net_pnl']), D(52))
            self.assertEqual(runner.executor.trades[0].redemption_prices_locked_at, self.entry)

    def test_missing_or_blocked_t_plus_one_sale_fails_before_etf_buy(self):
        for symbol in ('600000.SH', '000001.SZ'):
            rows = [r for r in self.rows(Side.REDEEM) if (r[0], r[1]) != (self.entry, symbol)]
            write_market(self.fixture.market, rows)
            config = replace(self.config, output_dir=self.fixture.root / symbol, quality_check=False)
            runner = make_full_runner(config, rules=self.fixture.rules)
            with self.assertRaisesRegex(ValueError, 'Next-minute redemption lacks an executable sale'):
                runner.run()
            self.assertFalse(runner.executor.fills)
            self.assertFalse(runner.executor.account.rows)
        self.fixture.rules.add(SecurityTradingRule('600000.SH', fixtures.DAYS[0], fixtures.DAYS[-1], True, False))
        write_market(self.fixture.market, self.rows(Side.REDEEM))
        runner = make_full_runner(replace(self.config, output_dir=self.fixture.root / 'blocked'),
                                  rules=self.fixture.rules)
        with self.assertRaisesRegex(ValueError, 'security_rule_blocked'):
            runner.run()
        self.assertFalse(runner.executor.fills)

    def test_cash_substitution_stays_unpaid_after_next_day_component_adjustment(self):
        config = replace(self.config, simulation=replace(self.config.simulation, cash_substitution_lag=None))
        runner, summary = self.fixture.run_model(self.rows(Side.REDEEM), config)
        cash, substitution = runner.executor.trades[0].legs
        self.assertTrue(cash.paid)
        self.assertFalse(substitution.paid)
        self.assertEqual(summary['pending_count'], 1)
        self.assertIsNone(summary['final_net_pnl'])

    def test_adjustment_shortfall_stays_payable_without_borrowing(self):
        self.pcf_field(1, 'CashComponent', -200)
        config = replace(self.config, starting_cash=D(315),
                         simulation=replace(self.config.simulation, cash_substitution_lag=None))
        runner, summary = self.fixture.run_model(self.rows(Side.REDEEM), config)
        leg = runner.executor.trades[0].legs[0]
        self.assertEqual(leg.final, D(-210))
        self.assertFalse(leg.paid)
        self.assertTrue(all(r['cash_balance'] >= 0 for r in runner.executor.account.rows))
        self.assertIsNone(summary['final_net_pnl'])

    def test_explicit_research_lag_overrides_default_next_day_adjustment(self):
        config = replace(self.config, simulation=replace(self.config.simulation, cash_difference_lag=1))
        runner, _ = self.fixture.run_model(self.rows(Side.CREATE), config)
        leg = runner.executor.trades[0].legs[0]
        self.assertEqual(leg.known_on, fixtures.DAYS[1])
        self.assertEqual(leg.paid_at, datetime.combine(fixtures.DAYS[2], time(9, 30)))
        self.assertEqual(leg.payment_basis, 'explicit_research_lag')

    def test_adjustment_uses_first_allowed_clock_instead_of_blocked_0930(self):
        clocks = tuple(time.fromisoformat(t) for t in ('09:30', '09:35', '09:36', '09:37', '14:30', '15:00'))
        rows = [(datetime.combine(day, clock), symbol, price)
                for day in fixtures.DAYS for clock in clocks
                for symbol, price in (('159338.SZ', 4.1), ('000001.SZ', 1), ('600000.SH', 2))]
        config = replace(self.config, clock_times=clocks, simulation=replace(self.config.simulation,
            blocked_trade_minutes=('09:30',), trading_sessions=(('09:35', '11:27'), ('13:00', '14:57'))))
        runner, _ = self.fixture.run_model(rows, config)
        self.assertEqual(runner.executor.trades[0].legs[0].paid_at,
                         datetime.combine(fixtures.DAYS[1], time(9, 35)))

    def test_cash_reconciliation_does_not_duplicate_transaction_fees(self):
        for side, pnl in ((Side.CREATE, '47.90'), (Side.REDEEM, '51.76')):
            config = replace(self.config, output_dir=self.fixture.root / side.value,
                             simulation=replace(self.config.simulation, fees=FeeRates()))
            runner, summary = self.fixture.run_model(self.rows(side), config)
            self.assertEqual(D(summary['final_net_pnl']), D(pnl))
            self.assertEqual(runner.executor.account.cash - config.starting_cash, D(pnl))
            self.assertEqual(len(runner.executor.fee_rows), 3)

    def test_formal_outputs_include_estimate_actual_difference_and_payment_basis(self):
        runner, _ = self.fixture.run_model(self.rows(Side.REDEEM), self.config)
        with (self.config.output_dir / 'cash_settlements.csv').open(encoding='utf-8-sig', newline='') as stream:
            row = next(r for r in csv.DictReader(stream) if r['kind'] == 'cash_difference')
        self.assertEqual([D(row[k]) for k in
                         ('estimated_component', 'actual_component', 'component_difference', 'initial_cash_flow', 'final')],
                         [D(10), D(12), D(2), D(10), D(2)])
        self.assertEqual(row['payment_basis'], 'next_day_adjustment_research')
        html = (self.config.output_dir / 'report/report.html').read_text(encoding='utf-8')
        self.assertIn('预估现金先交割、次日找补', html)
        self.assertIn('赎回代卖使用信号后 t+1 分钟', html)
        assumptions = json.loads((self.config.output_dir / 'assumptions.json').read_text(encoding='utf-8'))
        self.assertTrue(assumptions['estimated_cash_interim_settlement'])
        self.assertTrue(assumptions['redemption_sales_at_next_minute'])


if __name__ == '__main__':
    unittest.main()
