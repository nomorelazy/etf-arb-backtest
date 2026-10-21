import csv
import json
import io
from unittest.mock import patch
import unittest
from dataclasses import replace
from datetime import date, datetime, time
from decimal import Decimal as D

import test_full_flow as fixtures
from etf_arb.full_flow import make_full_runner
from etf_arb.rules.cash_substitution import CashSubstitutionTiming
from etf_arb.simulation_config import SimulationConfig
from etf_arb.rules import SecurityTradingRule
from test_missing_data import write_market


class CashTimingTests(unittest.TestCase):
    def test_attempt_time_is_an_explicit_minute_before_close(self):
        self.assertEqual(CashSubstitutionTiming().attempt_at(date(2025,1,6)),datetime(2025,1,6,14,30))
        self.assertIsNone(CashSubstitutionTiming().attempt_at(None))
        for invalid in ('14:30:00','9:30','15:00','24:00',None):
            with self.subTest(invalid=invalid), self.assertRaises((ValueError,TypeError)):
                SimulationConfig(cash_substitution_attempt_time=invalid)

    def test_notice_and_payment_bound_require_a_business_calendar(self):
        days = tuple(date(2025, 1, n) for n in (2, 3, 6, 7, 8, 9, 10))
        rule = CashSubstitutionTiming()
        self.assertEqual(rule.dates(days[0], days), (days[2], None, None))
        self.assertEqual(rule.dates(days[0], days, days), (days[2], days[3], days[6]))
        # Settlement workdays need not equal exchange trading dates.
        business = tuple(date(2025, 1, n) for n in (7, 9, 10, 13))
        self.assertEqual(rule.dates(days[0], days, business), (days[2], business[0], business[3]))


class RequirementsIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = replace(self.fixture.config, simulation=replace(self.fixture.config.simulation,
            cash_substitution_lag=None, cash_difference_lag=None))

    def run_model(self, config=None, rows=None):
        return self.fixture.run_model(rows, config or self.config)

    def test_priced_is_not_paid_but_does_not_block_later_hourly_cycles(self):
        runner, summary = self.run_model(replace(self.config, end_date=fixtures.DAYS[-1]))
        self.assertEqual(summary['opened_count'], 3)
        self.assertEqual(summary['pending_count'], 3)
        self.assertEqual(summary['settled_count'], 0)
        self.assertIsNone(summary['final_net_pnl'])
        self.assertTrue(all(leg.final is not None and not leg.paid for leg in runner.executor.trades[0].legs))
        self.assertFalse(any(row['event'] in {'CASH_SETTLED','CASH_SUBSTITUTION_SETTLED'} for row in runner.executor.account.rows))
        with self.assertRaises(ValueError):
            runner.resources.portfolio.total_net_pnl(fixtures.DAYS[0], fixtures.DAYS[-1])

    def test_explicit_payment_timestamps_are_posted_at_their_actual_time(self):
        payments = self.fixture.root/'payments.json'
        payment_day = date(2025,1,7)
        paid_at = datetime.combine(payment_day, time(9))
        template = (self.fixture.pcf/'159338/20250106.xml').read_text(encoding='utf-8')
        template = template.replace('<TradingDay>20250106</TradingDay>','<TradingDay>20250107</TradingDay>')
        template = template.replace('<PreTradingDay>20250103</PreTradingDay>','<PreTradingDay>20250106</PreTradingDay>')
        (self.fixture.pcf/'159338/20250107.xml').write_text(template,encoding='utf-8')
        config = replace(self.config, cash_payment_dates_path=payments,
                         trading_days=fixtures.DAYS+(payment_day,), settlement_end_date=payment_day)
        payments.write_text(json.dumps([dict(trade_id='T000001', kind=kind, paid_at=paid_at.isoformat())
            for kind in ('cash_difference','cash_substitution')]),encoding='utf-8')
        rows = self.fixture.market_rows()
        rows += [(datetime.combine(payment_day,clock), symbol,price)
                 for clock in fixtures.CLOCKS for symbol,price in (('159338.SZ',4.1),('000001.SZ',1),('600000.SH',2))]
        runner, summary = self.run_model(config,rows)
        self.assertEqual(summary['final_net_pnl'], '48.0')
        self.assertEqual(runner.resources.portfolio.trades[0].settled_at, paid_at)
        events = [r for r in runner.executor.account.rows if r['event'] in {'CASH_SETTLED','CASH_SUBSTITUTION_SETTLED'}]
        self.assertEqual([r['ts'] for r in events], [paid_at, paid_at])
        self.assertTrue(all(leg.paid_at == paid_at for leg in runner.executor.trades[0].legs))
        self.assertEqual(runner.executor.trades[0].legs[1].priced_at, datetime.combine(fixtures.DAYS[-1],time(15)))
        rows = runner.resources.portfolio.to_rows()
        self.assertEqual(rows[0]['交易额度（元）'], '410.0')
        self.assertEqual(rows[0]['交易单位（ETF份）'], '100')
        self.assertEqual(rows[0]['时间'], '2025-01-02 09:30:00')
        self.assertTrue((self.config.output_dir/'report/ledger.html').is_file())
        self.assertIn('逐笔全部现金', (self.config.output_dir/'report/report.html').read_text(encoding='utf-8'))

    def test_unfilled_proxy_remainder_is_valued_at_t_plus_two_close_without_a_fake_fill(self):
        settings = replace(self.config.simulation, quantity_units=2, max_shares_per_minute=100,
                           assume_full_basket_fill=False)
        rows = [r for r in self.fixture.market_rows() if r[1]!='600000.SH' or (
            r[0].date()==fixtures.DAYS[0] and r[0].time()<=time(9,33))]
        rows.append((datetime.combine(fixtures.DAYS[-1],time(15)), '600000.SH', 5))
        rows.append((datetime.combine(fixtures.DAYS[-1],time(14,30)), '600000.SH', 2))
        runner, summary = self.run_model(replace(self.config, simulation=settings, quality_check=False), rows)
        trade = runner.executor.trades[0]
        leg = next(leg for leg in trade.legs if leg.kind=='cash_substitution')
        self.assertEqual(leg.final, D('-254'))
        self.assertEqual(leg.priced_at, datetime.combine(fixtures.DAYS[-1],time(15)))
        self.assertFalse(leg.paid)
        self.assertIsNone(summary['final_net_pnl'])
        self.assertEqual(len([f for f in runner.executor.fills if f['purpose']=='MANAGER']),1)
        self.assertEqual(next(f['ts'] for f in runner.executor.fills if f['purpose']=='MANAGER'),datetime.combine(fixtures.DAYS[-1],time(14,30)))
        self.assertEqual(runner.executor.substitution_pricing[0]['quantity'],100)
        self.assertEqual(runner.executor.substitution_pricing[0]['price'],D('5'))
        self.assertEqual(runner.executor.substitution_attempts[0]['result'],'PARTIAL')
        self.assertEqual(runner.executor.substitution_attempts[0]['filled_quantity'],100)

    def test_both_directions_attempt_only_t_plus_two_1430_and_do_not_use_the_close_when_filled(self):
        at = datetime.combine(fixtures.DAYS[-1],time(14,30))
        for first,later,side,final in ((4,4.1,'BUY','-27'),(3.2,3.1,'SELL','300')):
            with self.subTest(side=side):
                rows = [(ts,symbol,2.5 if ts==at else 9 if ts==datetime.combine(fixtures.DAYS[-1],time(15)) else price)
                        if symbol=='600000.SH' else (ts,symbol,price)
                        for ts,symbol,price in self.fixture.market_rows(first,later)]
                runner, summary = self.run_model(replace(self.config,output_dir=self.fixture.root/side),rows)
                fills = [f for f in runner.executor.fills if f['purpose']=='MANAGER']
                self.assertEqual([(f['ts'],f['side'],f['price']) for f in fills],[(at,side,D('2.5'))])
                self.assertFalse(runner.executor.substitution_pricing)
                self.assertEqual(runner.executor.trades[0].legs[1].final,D(final))
                self.assertFalse(runner.executor.trades[0].legs[1].paid)
                self.assertIsNone(summary['final_net_pnl'])

    def test_missing_1430_bar_does_not_use_1429_or_retry_at_1431(self):
        day = fixtures.DAYS[-1]
        rows = [r for r in self.fixture.market_rows() if not(r[1]=='600000.SH' and r[0]==datetime.combine(day,time(14,30)))]
        rows += [(datetime.combine(day,clock),'600000.SH',1) for clock in (time(14,29),time(14,31))]
        rows = [(ts,symbol,5 if symbol=='600000.SH' and ts==datetime.combine(day,time(15)) else price)
                for ts,symbol,price in rows]
        config = replace(self.config,quality_check=False,
                         simulation=replace(self.config.simulation, max_trades_per_day=1),
                         clock_times=tuple(sorted(set(fixtures.CLOCKS+(time(14,29),time(14,31))))))
        runner,_ = self.run_model(config,rows)
        self.assertFalse(any(f['purpose']=='MANAGER' for f in runner.executor.fills))
        self.assertEqual(runner.executor.substitution_pricing[0]['price'],D('5'))
        self.assertEqual(runner.executor.trades[0].legs[1].final,D('-277'))
        self.assertEqual(runner.executor.substitution_attempts[0]['reason'],'missing_exact_bar')

    def test_supplied_trading_restriction_blocks_1430_but_allows_close_valuation(self):
        day = fixtures.DAYS[-1]
        self.fixture.rules.add(SecurityTradingRule('600000.SH',day,day,False,False))
        rows = [(ts,symbol,5 if symbol=='600000.SH' and ts==datetime.combine(day,time(15)) else price)
                for ts,symbol,price in self.fixture.market_rows()]
        runner,_ = self.run_model(rows=rows)
        self.assertFalse(any(f['purpose']=='MANAGER' for f in runner.executor.fills))
        self.assertEqual(runner.executor.substitution_pricing[0]['price'],D('5'))
        self.assertEqual(runner.executor.substitution_attempts[0]['reason'],'security_rule_blocked')
        ledger = (self.config.output_dir/'report/ledger.html').read_text(encoding='utf-8')
        self.assertIn('security_rule_blocked',ledger)

    def test_scheduled_attempt_works_without_a_1430_regular_clock(self):
        clocks = tuple(clock for clock in self.config.clock_times if clock!=time(14,30))
        runner,_ = self.run_model(replace(self.config,clock_times=clocks))
        self.assertEqual([f['ts'] for f in runner.executor.fills if f['purpose']=='MANAGER'],
                         [datetime.combine(fixtures.DAYS[-1],time(14,30))])

    def test_missing_deadline_close_keeps_amount_unresolved(self):
        rows = [r for r in self.fixture.market_rows() if r[1]!='600000.SH' or (
            r[0].date()==fixtures.DAYS[0] and r[0].time()<=time(9,31))]
        runner, summary = self.run_model(replace(self.config, quality_check=False), rows)
        self.assertEqual(runner.executor.trades[0].reason, 'missing_substitution_deadline_close')
        self.assertIsNone(runner.executor.trades[0].legs[1].final)
        self.assertIsNone(summary['final_net_pnl'])

    def test_threshold_is_explicit_and_can_change_between_runs(self):
        self.assertIsNone(SimulationConfig().premium_threshold)
        with self.assertRaisesRegex(ValueError, 'premium_threshold'):
            make_full_runner(replace(self.config,simulation=replace(self.config.simulation,premium_threshold=None)),rules=self.fixture.rules)
        for threshold, count in (('0.002',1),('0.004',0)):
            config = replace(self.config, output_dir=self.fixture.root/threshold,
                simulation=replace(self.config.simulation,premium_threshold=D(threshold)))
            _, summary = self.run_model(config, self.fixture.market_rows(3.61,3.61))
            self.assertEqual(summary['opened_count'], count)

    def test_redeem_six_column_amount_is_the_etf_buy_leg(self):
        runner, _ = self.run_model(self.fixture.config, self.fixture.market_rows(3.2,3.1))
        row = runner.resources.portfolio.to_rows()[0]
        self.assertEqual(row['交易类型'],'赎回')
        self.assertEqual(D(row['交易额度（元）']),D('310'))
        with (self.fixture.config.output_dir/'portfolio_ledger.csv').open(encoding='utf-8-sig',newline='') as handle:
            self.assertEqual(len(csv.DictReader(handle).fieldnames),6)

    def test_cli_threshold_override_does_not_modify_the_input_config(self):
        from etf_arb.cli import main
        rules = self.fixture.root/'rules.json'
        rules.write_text(json.dumps({'etf_rules':[dict(etf_code='159338',
            effective_from=fixtures.DAYS[0].isoformat(),effective_to=fixtures.DAYS[-1].isoformat(),
            creation_enabled=True,redemption_enabled=True)]}),encoding='utf-8')
        config = replace(self.config, rules_path=rules,
                         simulation=replace(self.config.simulation,premium_threshold=None))
        config_path = self.fixture.root/'input.json'
        original = json.dumps(config.to_dict())
        config_path.write_text(original,encoding='utf-8')
        write_market(self.fixture.market,self.fixture.market_rows())
        command = ['etf-arb','backtest','--config',str(config_path)]
        with patch('sys.argv',command), patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(main(),2)
        with patch('sys.argv',command+['--premium-threshold','0.2']), patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(main(),0)
        self.assertEqual(config_path.read_text(encoding='utf-8'),original)
        saved = json.loads((config.output_dir/'config.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['simulation']['premium_threshold'],'0.2')
