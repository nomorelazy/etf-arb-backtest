"""The configured interval is measured from submission, across both directions."""

import csv
import json
import unittest
from dataclasses import replace
from datetime import datetime, time, timedelta

import test_full_flow as fixtures
from etf_arb.runner_config import RunnerConfig


class DefaultFiveMinuteIntervalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_json_default_and_explicit_override_control_actual_applications(self):
        clocks = tuple(time(9, minute) for minute in range(35, 50)) + (
            time(10, 35), time(10, 36), time(10, 37), time(10, 38), time(14, 30), time(15))
        settings = replace(self.fixture.config.simulation, market_split_execution=True,
            entry_at_signal_time=True, cash_substitution_lag=None, cash_difference_lag=None)
        config = replace(self.fixture.config, clock_times=clocks, simulation=settings)
        payload = config.to_dict()
        payload['simulation'].pop('min_trade_interval_minutes')
        rows = [(datetime.combine(day, clock), symbol,
                 (4.1 if clock < time(9, 41) else 3.1 if clock < time(11) else 3.6)
                 if symbol == '159338.SZ' else price)
                for day in fixtures.DAYS for clock in clocks
                for symbol, price in (('159338.SZ', 0), ('000001.SZ', 1), ('600000.SH', 2))]
        for interval, signals, applied in (
            (None, ('09:35', '09:41', '09:47', '10:35'), ('09:36', '09:42', '09:48', '10:36')),
            (60, ('09:35', '10:36'), ('09:36', '10:37')),
        ):
            with self.subTest(interval=interval):
                raw = json.loads(json.dumps(payload))
                raw['output_dir'] = str(self.fixture.root / str(interval))
                if interval is not None:
                    raw['simulation']['min_trade_interval_minutes'] = interval
                path = self.fixture.root / f'config-{interval}.json'
                path.write_text(json.dumps(raw), encoding='utf-8')
                loaded = RunnerConfig.from_json(path)
                effective_interval = 5 if interval is None else interval
                self.assertEqual(loaded.simulation.min_trade_interval_minutes, effective_interval)
                runner, summary = self.fixture.run_model(rows, loaded)
                trades = runner.executor.trades
                self.assertEqual([trade.decision.ts.strftime('%H:%M') for trade in trades], list(signals))
                self.assertEqual([trade.applied_at.strftime('%H:%M') for trade in trades], list(applied))
                self.assertEqual(trades[0].decision.side.value, 'CREATE')
                self.assertTrue(all(trade.decision.side.value == 'REDEEM' for trade in trades[1:]))
                for prior, current in zip(trades, trades[1:]):
                    self.assertGreaterEqual(current.decision.ts - prior.applied_at,
                                            timedelta(minutes=effective_interval))
                self.assertEqual(summary['pending_count'], len(trades))
                self.assertEqual(summary['min_trade_interval_minutes'], effective_interval)
                with (loaded.output_dir / 'trades.csv').open(encoding='utf-8-sig', newline='') as stream:
                    exported = list(csv.DictReader(stream))
                self.assertEqual(exported[0]['next_signal_not_before'],
                                 '2025-01-02 09:41:00' if interval is None else '2025-01-02 10:36:00')
                report = (loaded.output_dir / 'report/report.html').read_text(encoding='utf-8')
                self.assertIn(f'满 {effective_interval} 分钟后', report)


if __name__ == '__main__':
    unittest.main()
