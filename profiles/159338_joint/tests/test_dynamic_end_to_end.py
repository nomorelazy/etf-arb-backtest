"""Exercise corrected cash estimates through signals, orders, settlement and report exports."""
import csv
from dataclasses import replace
from decimal import Decimal as D
import unittest

import test_full_flow as fixtures
from etf_arb.full_flow import make_full_runner
from test_dynamic_cash import write_holdings_xlsx
from test_missing_data import write_market


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class DynamicCashLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        start = fixtures.DAYS[0].isoformat()
        self.holdings = self.fixture.root / "holdings.xlsx"
        write_holdings_xlsx(self.holdings, [
            ("000001", 150, start), ("600000", 100, start),
            ("000002", 0, start), ("600001", 0, start),
        ])
        refs = self.fixture.root / "reference.csv"
        refs.write_text("time,thscode,preClose\n"
                        f"{start},000001.SZ,0.80\n"
                        f"{start},600000.SH,2.00\n", encoding="utf-8")
        self.config = replace(
            self.fixture.config,
            odd_lot_holdings_path=self.holdings,
            odd_lot_reference_prices_path=refs,
            # No spreadsheet on the second opening day. The first day's trade must still settle.
            end_date=fixtures.DAYS[1],
            simulation=replace(self.fixture.config.simulation,
                               quantity_units=2, max_trades_per_day=1,
                               entry_at_signal_time=True, market_split_execution=True),
        )

    def exercise(self, etf_price, side, edge, cash_effect, net_pnl):
        write_market(self.fixture.market, self.fixture.market_rows(etf_price, etf_price))
        runner = make_full_runner(self.config, rules=self.fixture.rules)
        summary = runner.run().financial_summary
        self.assertEqual(summary["opened_count"], 1)
        self.assertEqual(summary["settled_count"], 1)
        self.assertEqual(summary["pending_count"], 0)
        self.assertEqual(D(summary["final_net_pnl"]), net_pnl)
        out = self.config.output_dir
        signals = read_rows(out / "signals.csv")
        signal = next(row for row in signals if row["side"] == side)
        # Static cash is 10, the extra 50 shares gain 0.20 each; basket is 350 on 100 ETF shares.
        self.assertEqual(D(signal["iopv"]), D("3.7"))
        self.assertEqual(D(signal["pcf_cash_per_unit"]), D(10))
        self.assertEqual(D(signal["cash_adjustment_per_unit"]), D(10))
        self.assertEqual(D(signal["signal_cash_per_unit"]), D(20))
        self.assertEqual(signal["ts"], "2025-01-02 09:30:00")
        next_day = [row for row in signals if row["ts"].startswith(str(fixtures.DAYS[1]))]
        self.assertTrue(any(row["reason"].startswith("dynamic_unpriced:") for row in next_day))
        trade, = read_rows(out / "trade_attribution.csv")
        self.assertEqual(trade["side"], side)
        self.assertEqual(D(trade["estimated_cash"]), D(20))
        self.assertEqual(D(trade["signal_cash_adjustment"]), D(20))
        self.assertEqual(D(trade["signal_cash_estimate"]), D(40))
        self.assertEqual(D(trade["signal_gross_edge"]), edge)
        self.assertEqual(D(trade["actual_cash"]), D(24))  # Official 12 x 2, reported next day.
        self.assertEqual(D(trade["estimated_cash_pnl"]), cash_effect)
        self.assertEqual(D(trade["net_pnl"]), edge + cash_effect)
        flows = read_rows(out / "cashflows.csv")
        self.assertEqual(sum(D(row["cash_delta"]) for row in flows), net_pnl)
        self.assertEqual(D(summary["ending_cash"]), self.config.starting_cash + net_pnl)

    def test_dynamic_estimate_reverses_the_static_signal_and_cash_uses_the_pcf_settlement(self):
        # 3.65 is above static 3.60, but below dynamic 3.70. Redemption pays 3.65 for 200 shares.
        self.exercise(3.65, "REDEEM", D(10), D(-16), D(-6))

    def test_creation_cash_attribution_uses_reverse_sign_and_two_units(self):
        self.exercise(3.85, "CREATE", D(30), D(16), D(46))

    def test_exact_dynamic_redemption_threshold_is_eligible(self):
        # 3.6852 = 3.70 × (1 − 0.004). The dynamic rule includes an exact threshold hit.
        self.exercise(3.6852, "REDEEM", D("2.96"), D(-16), D("-13.04"))
