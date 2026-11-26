from decimal import Decimal
import unittest

from etf_arb.pricing import (
    PricingDataError,
    calculate_premium_discount,
    calculate_premium_discount_rate,
)


class PremiumDiscountTests(unittest.TestCase):
    def test_premium_discount_and_parity(self) -> None:
        cases = (
            ("1.02", "1.00", "0.02", "0.02"),
            ("0.98", "1.00", "-0.02", "-0.02"),
            ("1.00", "1.00", "0.00", "0"),
            ("3.00", "2.00", "1.00", "0.5"),
        )
        for price, iopv, difference, rate in cases:
            with self.subTest(price=price, iopv=iopv):
                result = calculate_premium_discount(Decimal(price), Decimal(iopv))
                self.assertEqual(result.price_difference, Decimal(difference))
                self.assertEqual(result.premium_discount_rate, Decimal(rate))

    def test_small_price_difference_preserves_decimal_precision(self) -> None:
        result = calculate_premium_discount(Decimal("1.000001"), Decimal("1"))
        self.assertEqual(result.price_difference, Decimal("0.000001"))
        self.assertEqual(f"{result.premium_discount_rate:.4%}", "0.0001%")

    def test_public_rate_interface_returns_percentage_points(self) -> None:
        cases = (
            ("1.02", "1.00", "2"),
            ("0.98", "1.00", "-2"),
            ("1.00", "1.00", "0"),
            ("3.00", "2.00", "50"),
            ("1.000001", "1", "0.0001"),
        )
        for price, iopv, expected in cases:
            with self.subTest(price=price, iopv=iopv):
                rate = calculate_premium_discount_rate(Decimal(price), Decimal(iopv))
                self.assertIsInstance(rate, Decimal)
                self.assertEqual(rate, Decimal(expected))
        self.assertEqual(
            f"{calculate_premium_discount_rate(Decimal('1.02'), Decimal('1')):.2f}%",
            "2.00%",
        )

    def test_rejects_invalid_prices_and_iopv(self) -> None:
        for value in ("0", "-1", "NaN", "sNaN", "Infinity", "-Infinity"):
            for field in ("etf_price", "iopv"):
                with self.subTest(value=value, field=field):
                    inputs = {"etf_price": Decimal("1"), "iopv": Decimal("1")}
                    inputs[field] = Decimal(value)
                    for calculate in (calculate_premium_discount, calculate_premium_discount_rate):
                        with self.assertRaises(PricingDataError):
                            calculate(**inputs)

    def test_rejects_non_decimal_inputs(self) -> None:
        for value in (1.02, "1.02", 1, True, None):
            for field in ("etf_price", "iopv"):
                with self.subTest(value=value, field=field):
                    inputs = {"etf_price": Decimal("1"), "iopv": Decimal("1")}
                    inputs[field] = value
                    for calculate in (calculate_premium_discount, calculate_premium_discount_rate):
                        with self.assertRaises(TypeError):
                            calculate(**inputs)


if __name__ == "__main__":
    unittest.main()
