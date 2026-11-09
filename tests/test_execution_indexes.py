from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
import unittest

from etf_arb.execution.simulator import ArbitrageSimulator
from etf_arb.portfolio.account import CashAccount
from etf_arb.portfolio.ledger import PortfolioLedger
from etf_arb.simulation_config import SimulationConfig


class ExecutionIndexTests(unittest.TestCase):
    def test_trade_order_query_preserves_global_order_ids_and_shared_order_state(self):
        simulator = ArbitrageSimulator(None, SimulationConfig())
        first = SimpleNamespace(trade_id="T1")
        second = SimpleNamespace(trade_id="T2")
        now = datetime(2025, 1, 2, 9, 35)
        simulator._order(first, "000001.SZ", "BUY", 100, now, "ENTRY")
        simulator._order(second, "159338.SZ", "BUY", 100, now, "ENTRY")
        simulator._order(first, "600000.SH", "BUY", 100, now, "MANAGER")
        simulator._order(first, "600000.SH", "BUY", 0, now, "MANAGER")

        self.assertEqual([order.order_id for order in simulator.orders],
                         ["O0000001", "O0000002", "O0000003"])
        snapshot = simulator._orders(first)
        self.assertEqual(snapshot, [simulator.orders[0], simulator.orders[2]])
        snapshot.clear()
        order, = simulator._orders(first, "MANAGER")
        order.cancelled = True
        self.assertIs(order, simulator.orders[2])
        self.assertEqual(simulator._orders(first, "MANAGER")[0].remaining, 0)
        self.assertEqual(simulator._orders(second), [simulator.orders[1]])

    def test_trade_holdings_isolate_shared_symbol_and_follow_fills_to_zero(self):
        account = CashAccount(Decimal("1000"), PortfolioLedger())
        now = datetime(2025, 1, 2, 9, 35)
        symbol, price = "000001.SZ", Decimal("1")
        account.fill(now, "T1", symbol, "BUY", 100, price)
        account.fill(now, "T2", symbol, "BUY", 200, price)
        account.fill(now, "T1", symbol, "SELL", 40, price)
        snapshot = account.holdings("T1")
        self.assertEqual(snapshot, {symbol: 60})
        snapshot.clear()
        with self.assertRaisesRegex(ValueError, "Insufficient shares"):
            account.fill(now, "T1", symbol, "SELL", 61, price)
        self.assertEqual(account.holdings("T1"), {symbol: 60})
        account.fill(now, "T1", symbol, "SELL", 60, price)
        self.assertEqual(account.holdings("T1"), {})
        self.assertEqual(account.positions["T1", symbol], 0)
        self.assertEqual(account.holdings("T2"), {symbol: 200})
        account.fill(now, "T1", symbol, "BUY", 25, price)
        self.assertEqual(account.holdings("T1"), {symbol: 25})
