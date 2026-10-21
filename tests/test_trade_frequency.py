from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest

from etf_arb.execution.simulator import ArbitrageSimulator
from etf_arb.rules import TradeFrequencyRule
from etf_arb.simulation_config import SimulationConfig


class TradeFrequencyTests(unittest.TestCase):
    def test_rolling_interval_does_not_reset_on_clock_hour(self):
        rule = TradeFrequencyRule()
        previous = datetime(2025, 1, 2, 10, 59)
        for minutes in (1, 30, 59):
            self.assertFalse(rule.allows(previous + timedelta(minutes=minutes), last_started_at=previous, started_today=1))
        self.assertTrue(rule.allows(previous + timedelta(minutes=60), last_started_at=previous, started_today=1))
        self.assertTrue(rule.allows(previous + timedelta(minutes=120), last_started_at=previous, started_today=5))

    def test_first_start_and_optional_daily_cap(self):
        at = datetime(2025, 1, 2, 9, 30)
        self.assertTrue(TradeFrequencyRule().allows(at, last_started_at=None, started_today=0))
        self.assertFalse(TradeFrequencyRule(max_trades_per_day=1).allows(at, last_started_at=None, started_today=1))

    def test_simulator_anchors_to_application_and_allows_unsettled_cycles(self):
        simulator = ArbitrageSimulator(None, SimulationConfig())
        previous = datetime(2025, 1, 2, 9, 30)
        trade = SimpleNamespace(state="APPLIED", applied_at=previous, decision=SimpleNamespace(ts=previous-timedelta(minutes=1)))
        simulator.trades.append(trade)
        simulator.started_by_day[previous.date()] = 1
        simulator.orders.extend([object()] * 500)
        self.assertFalse(simulator.available(previous + timedelta(minutes=59)))
        self.assertTrue(simulator.available(previous + timedelta(minutes=60)))
        self.assertTrue(simulator.available(previous + timedelta(hours=2)))
        trade.state = "CLOSED"
        self.assertFalse(simulator.available(previous + timedelta(minutes=59)))

    def test_signal_time_and_unsubmitted_entry_do_not_start_the_hour(self):
        simulator = ArbitrageSimulator(None, SimulationConfig())
        signal = datetime(2025,1,2,9,35)
        trade = SimpleNamespace(state="BUILD", applied_at=None, decision=SimpleNamespace(ts=signal))
        simulator.trades.append(trade)
        self.assertFalse(simulator.available(signal+timedelta(hours=2)))
        trade.applied_at = signal + timedelta(minutes=25)
        trade.state = "APPLIED"
        self.assertFalse(simulator.available(signal + timedelta(minutes=60)))
        self.assertFalse(simulator.available(trade.applied_at + timedelta(minutes=59)))
        self.assertTrue(simulator.available(trade.applied_at + timedelta(minutes=60)))

    def test_invalid_frequency_settings_are_rejected(self):
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                SimulationConfig(min_trade_interval_minutes=value)
        self.assertIsNone(SimulationConfig().max_trades_per_day)


if __name__ == "__main__":
    unittest.main()
