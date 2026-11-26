"""Check replay and report attribution use the stored signal-time cash estimate."""

import unittest
from decimal import Decimal as D

from etf_arb.report.attribution import attribution_html, build_trade_attribution
from etf_arb.report.rebuild import restore_signal_references


class DynamicCashAttributionTests(unittest.TestCase):
    def setUp(self):
        self.trade = dict(trade_id="T000001", side="REDEEM", state="APPLIED",
                          opened_at="2025-06-09 09:35:00", quantity_units=3, etf_shares=300,
                          estimated_cash="-9", signal_cash_estimate="0", signal_gross_edge="18",
                          substitution_deposit="0", domestic_fixed_cash="0")
        self.signal = dict(ts=self.trade["opened_at"], etf_close="0.94", iopv="1.00",
                           premium="-0.06", signal_cash_per_unit="0")
        self.leg = dict(trade_id=self.trade["trade_id"], kind="cash_difference", final="12")

    def attribution(self):
        return build_trade_attribution([self.trade], [self.signal], [], [self.leg], [])[0]

    def test_dynamic_zero_and_legacy_cash_leg_reconstruction_use_different_bases(self):
        self.trade.update(estimated_cash="9")
        # Legacy REDEEM receives the official component plus its nine-yuan advance back.
        row = self.attribution()
        self.assertEqual(row["actual_cash"], D("3"))
        self.assertEqual(row["signal_cash_estimate"], D("0"))
        self.assertEqual(row["signal_cash_adjustment"], D("-9"))
        self.assertEqual(row["estimated_cash_pnl"], D("3"))
        self.assertIsNone(row["net_pnl"])
        self.assertIn("信号动态现金估计", attribution_html([row]))

    def test_signal_snapshot_restores_missing_trade_field_and_rejects_conflict(self):
        self.trade.pop("signal_cash_estimate")
        self.signal["signal_cash_per_unit"] = "-4"
        self.assertEqual(self.attribution()["signal_cash_estimate"], D("-12"))
        self.trade["signal_cash_estimate"] = "-4"
        with self.assertRaisesRegex(ValueError, "Conflicting signal cash"):
            self.attribution()

    def test_report_rebuild_uses_saved_decision_without_loading_future_or_current_inputs(self):
        self.trade.pop("signal_cash_estimate")
        self.trade.update(signal_physical_value="0", signal_manager_value="0", signal_fixed_cash="0",
                          signal_physical_quantity=0)
        decision = dict(ts=self.trade["opened_at"], side="REDEEM", gross_edge="18", signal_cash_per_unit="-4")
        restore_signal_references({"odd_lot_holdings_path": "unused.xlsx"}, [self.trade], [decision], [])
        self.assertEqual(self.trade["signal_cash_estimate"], D("-12"))
