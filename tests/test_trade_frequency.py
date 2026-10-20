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

    def test_simulator_counts_cycles_not_constituent_orders_and_retains_active_guard(self):
        simulator = ArbitrageSimulator(None, SimulationConfig())
        previous = datetime(2025, 1, 2, 9, 30)
        trade = SimpleNamespace(state="CLOSED", decision=SimpleNamespace(ts=previous))
        simulator.trades.append(trade)
        simulator.started_by_day[previous.date()] = 1
        simulator.orders.extend([object()] * 500)
        self.assertFalse(simulator.available(previous + timedelta(minutes=59)))
        self.assertTrue(simulator.available(previous + timedelta(minutes=60)))
        trade.state = "APPLIED"
        self.assertFalse(simulator.available(previous + timedelta(hours=2)))

    def test_invalid_frequency_settings_are_rejected(self):
        for value in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                SimulationConfig(min_trade_interval_minutes=value)
        self.assertIsNone(SimulationConfig().max_trades_per_day)


if __name__ == "__main__":
    unittest.main()
