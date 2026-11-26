import csv
from dataclasses import replace
from datetime import datetime, time
from decimal import Decimal
import unittest

import test_full_flow as fixtures
from etf_arb.report.rebuild import rebuild_run_report


class PriceLimitFullFlowTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FullFlowTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bounds = self.fixture.root / "limits.csv"
        self.bounds.write_text(
            "date,symbol,upper_limit,lower_limit\n"
            "2025-01-02,000001.SZ,1.10,0.90\n"
            "2025-01-02,600000.SH,2.20,1.80\n",
            encoding="utf-8",
        )
        config = self.fixture.config
        self.config = replace(config, filter_price_limits=True, daily_price_limits_path=self.bounds,
                              simulation=replace(config.simulation, market_split_execution=True,
                                                 entry_at_signal_time=True, max_trades_per_day=1))

    def test_limit_up_and_limit_down_block_both_directions_before_buying(self):
        for side, etf_price in (("CREATE", 4), ("REDEEM", 3)):
            for price, reason in ((1.1, "limit_up:provided"), (.9, "limit_down:provided")):
                with self.subTest(side=side, reason=reason):
                    config = replace(self.config, output_dir=self.fixture.root / f"{side}-{price}")
                    rows = [(at, symbol, price if symbol == "000001.SZ" else close)
                            for at, symbol, close in self.fixture.market_rows(etf_price, etf_price)]
                    runner, summary = self.fixture.run_model(rows, config)
                    self.assertEqual(summary["opened_count"], 0)
                    self.assertFalse(runner.executor.fills)
                    with (config.output_dir / "price_limit_blocks.csv").open(encoding="utf-8-sig") as stream:
                        blocks = list(csv.DictReader(stream))
                    self.assertTrue(blocks)
                    self.assertEqual({row["side"] for row in blocks}, {side})
                    self.assertTrue(all(reason in row["component_reasons"] for row in blocks))

    def test_new_limit_at_application_keeps_prior_entry_and_counts_unwind(self):
        for side, etf_price, expected_pnl in (("CREATE", 4, 3), ("REDEEM", 3, 5)):
            with self.subTest(side=side):
                config = replace(self.config, output_dir=self.fixture.root / side)
                signal = datetime.combine(fixtures.DAYS[0], time(9, 30))
                rows = [
                    (at, symbol, 1.1 if symbol == "000001.SZ" and at.date() == signal.date() and at > signal
                     else 1.03 if symbol == "000001.SZ" and at.date() == fixtures.DAYS[1]
                     else 3.05 if side == "REDEEM" and symbol == "159338.SZ" and at.date() == fixtures.DAYS[1]
                     else close)
                    for at, symbol, close in self.fixture.market_rows(etf_price, etf_price)
                ]
                runner, summary = self.fixture.run_model(rows, config)
                self.assertEqual(summary["price_limit_application_blocks"], 1)
                self.assertEqual(summary["submitted_creation_count"], 0)
                self.assertEqual(summary["submitted_redemption_count"], 0)
                trade, = runner.executor.trades
                self.assertIsNone(trade.applied_at)
                self.assertEqual(trade.state, "UNWOUND")
                self.assertEqual([fill["purpose"] for fill in runner.executor.fills], ["ENTRY", "UNWIND"])
                self.assertEqual(runner.executor.fills[0]["ts"], signal)
                self.assertEqual(runner.executor.fills[1]["ts"].date(), fixtures.DAYS[1])
                self.assertEqual(Decimal(summary["final_net_pnl"]), expected_pnl)
                self.assertEqual(runner.resources.portfolio.to_rows(), [])
                self.assertFalse(trade.legs)
                rebuilt = rebuild_run_report(config.output_dir, self.fixture.root / f"rebuild-{side}")
                self.assertTrue(rebuilt.is_file())

    def test_missing_previous_close_is_recorded_and_estimates_use_previous_day(self):
        config = replace(self.config, daily_price_limits_path=None, quality_check=False)
        # Both reference sources must be absent; a valid 09:30 close now
        # intentionally supplies the explicitly requested opening proxy.
        missing = [row for row in self.fixture.market_rows() if not (
            row[0] == datetime.combine(fixtures.DAYS[0], time(9, 30))
            and row[1] == "000001.SZ")]
        runner, summary = self.fixture.run_model(missing, config)
        self.assertFalse(runner.executor.fills)
        self.assertGreater(summary["price_limit_unknown_signal_blocks"], 0)

        next_day = fixtures.DAYS[1]
        config = replace(config, start_date=next_day, end_date=next_day,
                         output_dir=self.fixture.root / "estimated")
        rows = [(at, symbol, 1.1 if symbol == "000001.SZ" and at.date() == next_day else close)
                for at, symbol, close in self.fixture.market_rows()]
        runner, summary = self.fixture.run_model(rows, config)
        self.assertFalse(runner.executor.fills)
        self.assertGreater(summary["price_limit_signal_blocks"], 0)
