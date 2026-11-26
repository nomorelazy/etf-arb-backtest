import csv
import json
from dataclasses import replace
from decimal import Decimal as D
import unittest

import test_full_flow as fixture
from etf_arb.domain import Side
from etf_arb.rules.limit_loss_calibration import estimate_limit_loss


class SignalLimitLossTests(unittest.TestCase):
    def test_creation_and_redemption_use_cash_hurdle_before_execution(self):
        for side, etf_at_first, etf_after, sh_upper, sh_lower in (
            (Side.CREATE, 3.62, 4.1, 2, 1.7),
            (Side.REDEEM, 3.58, 3.1, 2.3, 2),
        ):
            with self.subTest(side=side):
                base = fixture.FullFlowTests()
                base.setUp()
                self.addCleanup(base.doCleanups)
                path = base.root / "daily_limits.csv"
                # SZ is at the opposite limit. Only SH contributes on the first minute.
                sz_bounds = "1.1,1" if side is Side.CREATE else "1,0.9"
                path.write_text(
                    "date,symbol,upper_limit,lower_limit\n"
                    f"2025-01-02,600000.SH,{sh_upper},{sh_lower}\n"
                    f"2025-01-02,000001.SZ,{sz_bounds}\n", encoding="utf8")
                config = replace(base.config, daily_price_limits_path=path,
                                 simulation=replace(base.config.simulation, limit_loss_threshold=True,
                                                    quantity_units=4, market_split_execution=True,
                                                    entry_at_signal_time=True))
                rows = base.market_rows(first=etf_at_first, later=etf_after)
                # SH unlocks at the next minute; prices remain strictly inside the bounds.
                rows = [(at, symbol, (1.99 if side is Side.CREATE else 2.01)
                         if symbol == "600000.SH" and at.time() != fixture.CLOCKS[0] else price)
                        for at, symbol, price in rows]
                runner, summary = base.run_model(rows, config)
                with (config.output_dir / "signals.csv").open(encoding="utf-8-sig", newline="") as handle:
                    signals = list(csv.DictReader(handle))
                rejected, opened = signals[:2]
                self.assertEqual(rejected["reason"], "below_limit_loss_threshold")
                self.assertEqual(D(rejected["base_threshold"]), D("0.004"))
                self.assertEqual(D(rejected["signal_basket_value"]), D(1440))
                projected = estimate_limit_loss(side, "SH", D(800))
                self.assertEqual(D(rejected["estimated_limit_loss"]), projected.expected_loss)
                self.assertEqual(D(rejected["effective_threshold"]),
                                 D("0.004") + projected.expected_loss / D(1440))
                stocks = json.loads(rejected["limit_loss_components"])
                self.assertEqual([r["symbol"] for r in stocks], ["600000.SH"])
                self.assertEqual(D(stocks[0]["quantity"]), D(400))
                self.assertEqual(opened["reason"], "threshold_crossed")
                self.assertEqual(D(opened["estimated_limit_loss"]), 0)
                self.assertEqual(runner.executor.trades[0].decision.ts.isoformat(" "), opened["ts"])
                self.assertGreaterEqual(summary["limit_loss_blocks"], 1)
                self.assertIn("样本内对照", (config.output_dir / "report" / "report.html").read_text(encoding="utf8"))

