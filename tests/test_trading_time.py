from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal as D
import json
import unittest

from etf_arb.domain import Side, TradeDecision
from etf_arb.full_flow import make_full_runner
from etf_arb.rules import ETFRule, InMemoryRuleRepository, TradingTimeRule
from etf_arb.simulation_config import SimulationConfig
from etf_arb.runner_config import RunnerConfig
import test_full_flow as fixtures
from test_full_flow import DAYS
from test_missing_data import write_market


class TradingTimeUnitTests(unittest.TestCase):
    def test_exact_user_sessions_and_boundaries(self):
        rule = TradingTimeRule()
        for clock in (time(9,30), time(9,34,59), time(11,30), time(12,59), time(13,2,59), time(14,58), time(15)):
            self.assertFalse(rule.allows(clock), clock)
        for clock in (time(9,35), time(11,28), time(11,29), time(11,29,59), time(13,3), time(14,57,59)):
            self.assertTrue(rule.allows(clock), clock)
        self.assertTrue(rule.has_later_minute_today(time(11,27)))
        self.assertFalse(rule.has_later_minute_today(time(14,57)))

    def test_session_validation_and_json_lists(self):
        self.assertEqual(SimulationConfig(trading_sessions=[["09:35","11:27"],["13:00","14:57"]]).trading_sessions,
                         (("09:35","11:27"),("13:00","14:57")))
        for sessions in ([], [["09:35","09:30"]], [["09:35","11:27"],["11:27","14:57"]], [["9:35","11:27"]]):
            with self.assertRaises(ValueError):
                SimulationConfig(trading_sessions=sessions)


class TradingTimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Reuse only the fixture builder, not its baseline test methods.
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.clocks = (time(9,30),time(9,34),time(9,35),time(9,36),time(9,37),
                       time(11,27),time(11,28),time(13),time(14,55),time(14,56),time(14,57),time(14,58),time(15))
        self.config = replace(self.fixture.config, clock_times=self.clocks,
                              simulation=SimulationConfig(premium_threshold=D("0.004"), cash_substitution_lag=2, cash_difference_lag=1, security_rule_policy="assume_tradable", market_split_execution=False, entry_at_signal_time=False))
        self.rules = InMemoryRuleRepository([ETFRule("159338",DAYS[0],DAYS[-1],True,True,time(14,57),time(14,57))])

    def rows(self, etf_price):
        return [(datetime.combine(day,clock),symbol,
                 etf_price(day,clock) if symbol=="159338.SZ" else price)
                for day in DAYS for clock in self.clocks
                for symbol,price in (("159338.SZ",0),("000001.SZ",1),("600000.SH",2))]

    def test_source_executor_and_cash_events_all_follow_sessions(self):
        write_market(self.fixture.market,self.rows(lambda d,t:4.1))
        runner=make_full_runner(self.config,rules=self.rules)
        manual=TradeDecision("159338",datetime(2025,1,2,9,30),True,Side.CREATE,1,D("1"),D("0"),D("1"))
        result=runner.run([manual])
        self.assertEqual(result.financial_summary['opened_count'],3)
        self.assertEqual(runner.executor.trades[0].decision.ts.time(),time(9,35))
        self.assertEqual(runner.executor.fills[0]['ts'].time(),time(9,36))
        self.assertEqual(runner.executor.rejections[0]['reason'],'blocked_trade_minute')
        rule=TradingTimeRule()
        self.assertTrue(all(rule.allows(f['ts'].time()) for f in runner.executor.fills))
        self.assertTrue(all(rule.allows(e.ts.time()) for e in runner.resources.portfolio.events
                            if e.event_type.value not in {'CASH_SETTLED','CASH_SUBSTITUTION_SETTLED'}))
        self.assertEqual(next(r for r in runner.source.rows if r['ts'].time()==time(13))['reason'],'outside_trading_sessions')
        html=(self.config.output_dir/'report/report.html').read_text(encoding='utf-8')
        self.assertTrue(html.startswith('<!doctype html>'))
        self.assertNotIn('<aside',html)

    def test_1457_fill_is_allowed_and_exit_waits_for_next_user_session(self):
        write_market(self.fixture.market,self.rows(lambda d,t:3.2 if d==DAYS[0] and t>=time(14,56) else 3.6))
        runner=make_full_runner(self.config,rules=self.rules)
        result=runner.run()
        self.assertEqual(result.financial_summary['opened_count'],1)
        self.assertEqual(runner.executor.trades[0].decision.ts.time(),time(14,56))
        self.assertEqual(runner.executor.fills[0]['ts'].time(),time(14,57))
        self.assertEqual(next(f for f in runner.executor.fills if f['purpose']=='EXIT')['ts'],datetime(2025,1,3,9,35))

    def test_last_minute_does_not_open_an_entry_that_cannot_fill_today(self):
        write_market(self.fixture.market,self.rows(lambda d,t:4.1 if d==DAYS[0] and t==time(14,57) else 3.6))
        runner=make_full_runner(self.config,rules=self.rules)
        result=runner.run()
        self.assertEqual(result.financial_summary['opened_count'],0)
        row=next(r for r in runner.source.rows if r['ts'].time()==time(14,57))
        self.assertEqual(row['reason'],'no_later_entry_minute_today')

    def test_json_defaults_and_explicit_override_reach_actual_trades(self):
        self.clocks = tuple(time.fromisoformat(value) for value in (
            '09:30', '09:31', '09:34', '09:35', '09:36',
            '11:28', '11:29', '11:30', '13:00', '13:01', '13:02',
            '13:03', '13:04', '14:56', '14:57', '14:58', '15:00'))
        write_market(self.fixture.market, self.rows(lambda day, clock: 4.1))
        # Exercise the current t-entry / t+1 application path, not only legacy fills.
        settings = replace(self.config.simulation, market_split_execution=True, entry_at_signal_time=True)
        raw = replace(self.config, clock_times=self.clocks, simulation=settings).to_dict()
        raw['simulation'].pop('trading_sessions')
        cases = (
            ('defaults', None, ('09:35', '11:28', '13:03', '14:56')),
            ('override', [['09:30', '11:30'], ['13:00', '14:57']], ('09:30', '11:28', '13:00', '14:56')),
        )
        for name, sessions, expected in cases:
            with self.subTest(name=name):
                payload = json.loads(json.dumps(raw))
                payload['output_dir'] = str(self.fixture.root / name)
                if sessions is not None:
                    payload['simulation']['trading_sessions'] = sessions
                    payload['simulation']['blocked_trade_minutes'] = []
                path = self.fixture.root / (name + '.json')
                path.write_text(json.dumps(payload), encoding='utf-8')
                config = RunnerConfig.from_json(path)
                runner = make_full_runner(config, rules=self.rules)
                runner.run()
                self.assertEqual([trade.decision.ts.strftime('%H:%M') for trade in runner.executor.trades], list(expected))
                self.assertEqual([trade.applied_at.strftime('%H:%M') for trade in runner.executor.trades],
                                 ['09:36' if name == 'defaults' else '09:31', '11:29',
                                  '13:04' if name == 'defaults' else '13:01', '14:57'])
                rule = TradingTimeRule(config.simulation.blocked_trade_minutes, config.simulation.trading_sessions)
                self.assertTrue(all(rule.allows(fill['ts'].time()) for fill in runner.executor.fills
                                    if fill['purpose'] != 'MANAGER'))
                if name == 'defaults':
                    for clock in ('09:34', '11:30', '13:00', '13:01', '13:02', '14:58'):
                        row = next(row for row in runner.source.rows if row['ts'].strftime('%H:%M') == clock)
                        self.assertEqual(row['reason'], 'outside_trading_sessions')


if __name__=='__main__':
    unittest.main()
