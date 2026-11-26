import json
from dataclasses import replace
from datetime import time
from decimal import Decimal as D
import unittest
import xml.etree.ElementTree as ET

import test_full_flow as fixtures
from etf_arb.full_flow import make_full_runner
from etf_arb.simulation_config import SimulationConfig
from test_missing_data import write_market


class FullBasketExecutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_default_is_full_basket_and_own_orders_ignore_partial_capacity(self):
        self.assertTrue(SimulationConfig().assume_full_basket_fill)
        config = replace(self.fixture.config, simulation=replace(self.fixture.config.simulation,
            quantity_units=2, max_shares_per_minute=100))
        runner, summary = self.fixture.run_model(config=config)
        own_fills = [row for row in runner.executor.fills if row["purpose"] != "MANAGER"]
        self.assertEqual([(row["symbol"], row["quantity"]) for row in own_fills],
                         [("000001.SZ", 200), ("159338.SZ", 200)])
        self.assertEqual(runner.executor.trades[0].applied_at.time(), time(9, 31))
        self.assertEqual(D(summary["final_net_pnl"]), D("96"))
        self.assertEqual(summary["held_pending_policy_count"], 0)
        # The manager attempt is still a distinct policy: its cap is retained.
        manager = [row for row in runner.executor.fills if row["purpose"] == "MANAGER"]
        self.assertEqual([row["quantity"] for row in manager], [100])
        self.assertEqual(runner.executor.substitution_pricing[0]["quantity"], 100)

    def test_all_entries_are_preflighted_before_any_cash_or_security_fill(self):
        for path in (self.fixture.pcf / "159338").glob("*.xml"):
            tree = ET.parse(path)
            nodes = tree.find("Components")
            nodes.append(ET.fromstring("""<Component>
                <UnderlyingSecurityID>000003</UnderlyingSecurityID><UnderlyingSecurityIDSource>102</UnderlyingSecurityIDSource>
                <ComponentShare>100</ComponentShare><SubstituteFlag>0</SubstituteFlag></Component>"""))
            tree.write(path, encoding="utf-8")
        rows = self.fixture.market_rows(first=6, later=6)
        extra = [(ts, "000003.SZ", 1.5 if ts.time() == time(9,30) else 3)
                 for ts, code, _ in rows if code == "000001.SZ"]
        write_market(self.fixture.market, rows + extra)
        runner = make_full_runner(replace(self.fixture.config, starting_cash=D("650")), rules=self.fixture.rules)
        with self.assertRaisesRegex(ValueError, "Full-basket assumption cannot be funded"):
            runner.run()
        self.assertEqual(runner.executor.fills, [])
        self.assertEqual(runner.executor.account.rows, [])
        self.assertEqual(runner.executor.account.cash, D("650"))
        self.assertEqual(runner.executor.account.holdings("T000001"), {})
        self.assertFalse(any(trade.state == "HELD_PENDING_POLICY" for trade in runner.executor.trades))
        summary = json.loads((self.fixture.config.output_dir / "run_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["state"], "failed")

    def test_missing_entry_after_signal_fails_without_partial_holdings(self):
        rows = [row for row in self.fixture.market_rows() if not (
            row[1] == "000001.SZ" and row[0].date() == fixtures.DAYS[0] and row[0].time() == time(9,31))]
        write_market(self.fixture.market, rows)
        config = replace(self.fixture.config, quality_check=False)
        runner = make_full_runner(config, rules=self.fixture.rules)
        with self.assertRaisesRegex(ValueError, "missing_exact_bar"):
            runner.run()
        self.assertEqual(runner.executor.fills, [])
        self.assertEqual(runner.executor.account.rows, [])
        self.assertEqual(runner.executor.account.holdings("T000001"), {})

    def test_cash_uses_the_execution_close_and_reserves_the_full_application(self):
        rows = [(ts, symbol, 1.2 if symbol == "000001.SZ" and ts.time() > time(9,30) else price)
                for ts, symbol, price in self.fixture.market_rows()]
        runner, summary = self.fixture.run_model(rows)
        flows = runner.executor.account.rows
        self.assertEqual(flows[0]["cash_delta"], D("-120"))
        self.assertEqual(flows[1]["cash_delta"], D("-283"))
        self.assertEqual(D(summary["final_net_pnl"]), D("28"))
        self.assertEqual(summary["held_pending_policy_count"], 0)

    def test_full_basket_setting_roundtrips_and_rejects_non_boolean(self):
        path = self.fixture.root / "config.json"
        path.write_text(json.dumps(self.fixture.config.to_dict()), encoding="utf-8")
        self.assertEqual(type(self.fixture.config).from_json(path), self.fixture.config)
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SimulationConfig(assume_full_basket_fill=value)


if __name__ == "__main__":
    unittest.main()
