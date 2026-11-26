from decimal import Decimal
import unittest

from etf_arb.domain import Side
from etf_arb.rules.limit_loss_calibration import estimate_limit_loss


class LimitLossCalibrationTests(unittest.TestCase):
    def test_unrounded_weighted_bucket_rate_and_loss(self):
        result = estimate_limit_loss(Side.CREATE, "SZ", Decimal("5000"))
        self.assertEqual((5000, 10000, 75), (result.bucket_min, result.bucket_max, result.sample_count))
        self.assertEqual(result.expected_loss, Decimal(5000 * 41135) / 530826)
        self.assertEqual(result.loss_per_100, Decimal(100 * 41135) / 530826)
        self.assertFalse(result.used_group_fallback)

    def test_inclusive_minima_opposite_sign_and_zero_value(self):
        self.assertEqual(estimate_limit_loss(Side.CREATE, "SZ", Decimal("999.99")).expected_loss, 0)
        for lower in (0, 1000, 3000, 5000, 10000, 20000):
            self.assertEqual(estimate_limit_loss(Side.CREATE, "SZ", Decimal(lower)).bucket_min, lower)
        for exchange in ("SH", "SZ"):
            result = estimate_limit_loss(Side.REDEEM, exchange, Decimal("20000"))
            self.assertEqual(result.expected_loss, 0)
            self.assertIsNone(result.bucket_max)

    def test_empty_buckets_use_value_weighted_group_rate(self):
        for side, loss, value in ((Side.REDEEM, 23601, 970022), (Side.CREATE, 79277, 2129111)):
            result = estimate_limit_loss(side, "SH", Decimal("800"))
            self.assertEqual(result.sample_count, 0)
            self.assertTrue(result.used_group_fallback)
            self.assertEqual(result.expected_loss, Decimal(800 * loss) / value)
            self.assertEqual(estimate_limit_loss(side, "SH", Decimal(0)).expected_loss, 0)

    def test_invalid_stock_values_and_market(self):
        for value in ("NaN", "Infinity", "-1"):
            with self.assertRaises(ValueError):
                estimate_limit_loss(Side.REDEEM, "SH", Decimal(value))
        with self.assertRaises(ValueError):
            estimate_limit_loss(Side.CREATE, "HK", Decimal("1000"))
