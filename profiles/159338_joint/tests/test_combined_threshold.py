"""Both threshold modules through the full runner, cash settlement and exported signal audit."""

from dataclasses import replace
from decimal import Decimal as D
import json
import unittest

import test_dynamic_end_to_end as dynamic_fixture
import test_full_flow as fixture
from etf_arb.domain import Side
from etf_arb.rules.limit_loss_calibration import estimate_limit_loss


class CombinedThresholdTests(unittest.TestCase):
    def setUp(self):
        dynamic = dynamic_fixture.DynamicCashLifecycleTests()
        dynamic.setUp()
        self.addCleanup(dynamic.doCleanups)
        self.base = dynamic.fixture
        self.config = replace(dynamic.config, end_date=fixture.DAYS[0])
        self.bounds = self.base.root / "daily_limits.csv"

    def test_joint_threshold_rejects_then_opens_and_never_books_forecast_loss(self):
        # PCF-only numerator 360, dynamic numerator 370, two creation units.
        for side, upper, lower, first, later in (
            (Side.CREATE, 2, 1.8, 3.75, 3.90),
            (Side.REDEEM, 2.2, 2, 3.65, 3.50),
        ):
            with self.subTest(side=side):
                self.bounds.write_text(
                    "date,symbol,upper_limit,lower_limit\n"
                    f"2025-01-02,600000.SH,{upper},{lower}\n"
                    "2025-01-02,000001.SZ,1.1,0.9\n", encoding="utf-8")
                joint = replace(self.config, daily_price_limits_path=self.bounds,
                                output_dir=self.base.root / f"joint-{side.value}",
                                simulation=replace(self.config.simulation, limit_loss_threshold=True))
                # The first two snapshots hit the same limit. The stock then unlocks.
                rows = [(at, symbol, (1.99 if side is Side.CREATE else 2.01)
                         if symbol == "600000.SH" and at.time() > fixture.CLOCKS[1] else price)
                        for at, symbol, price in self.base.market_rows(first, later)]
                runner, summary = self.base.run_model(rows, joint)
                signals = dynamic_fixture.read_rows(joint.output_dir / "signals.csv")
                rejected, opened = signals[:2]
                self.assertEqual((rejected["reason"], opened["reason"]),
                                 ("below_limit_loss_threshold", "threshold_crossed"))
                self.assertEqual(runner.executor.trades[0].decision.ts.isoformat(" "), opened["ts"])
                loss = estimate_limit_loss(side, "SH", D(400)).expected_loss
                self.assertGreater(loss, 0)
                for row in (rejected, opened):
                    self.assertEqual(row["side"], side.value)
                    self.assertEqual(D(row["iopv"]), D("3.7"))
                    self.assertEqual(D(row["cash_adjustment_per_unit"]), D(10))
                    self.assertEqual(D(row["signal_basket_value"]), D(740))
                    self.assertEqual(D(row["estimated_limit_loss"]), loss)
                    self.assertEqual(D(row["effective_threshold"]), D("0.004") + loss / 740)
                    component, = json.loads(row["limit_loss_components"])
                    self.assertEqual(component["symbol"], "600000.SH")
                self.assertEqual(summary["settled_count"], 1)
                self.assertGreaterEqual(summary["limit_loss_blocks"], 1)
                trade, = dynamic_fixture.read_rows(joint.output_dir / "trade_attribution.csv")
                self.assertEqual(D(trade["signal_cash_estimate"]), D(40))
                self.assertEqual(D(trade["actual_cash"]), D(24))
                # A dynamic-only control with a 3% base opens at the same second snapshot.
                # Its identical fills and cash flows isolate the forecast's effect to the signal gate.
                control = replace(joint, output_dir=self.base.root / f"control-{side.value}",
                                  simulation=replace(joint.simulation, limit_loss_threshold=False,
                                                     premium_threshold=D("0.03")))
                _, control_summary = self.base.run_model(rows, control)
                self.assertEqual(summary["final_net_pnl"], control_summary["final_net_pnl"])
                for name in ("fills.csv", "cashflows.csv", "portfolio_ledger.csv"):
                    self.assertEqual(dynamic_fixture.read_rows(joint.output_dir / name),
                                     dynamic_fixture.read_rows(control.output_dir / name))

    def test_joint_threshold_requires_a_strict_crossing_with_zero_limit_loss(self):
        for side, boundary in ((Side.CREATE, 3.7148), (Side.REDEEM, 3.6852)):
            with self.subTest(side=side):
                # 3.7 × (1 ± 0.004). Dynamic alone includes equality; the combined rule skips it.
                rows = self.base.market_rows(boundary, boundary)
                for limit_enabled, expected_opens in ((False, 1), (True, 0)):
                    config = replace(self.config,
                                     output_dir=self.base.root / f"{side.value}-{limit_enabled}",
                                     simulation=replace(self.config.simulation, limit_loss_threshold=limit_enabled))
                    _, summary = self.base.run_model(rows, config)
                    self.assertEqual(summary["opened_count"], expected_opens)
