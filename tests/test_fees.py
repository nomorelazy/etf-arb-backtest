from dataclasses import replace
from decimal import Decimal as D
import unittest

from etf_arb.costs import AssetType, FeeRates, TradeFeeModel
from etf_arb.execution.models import OrderSide
from etf_arb.domain import Side


class FeeModelTests(unittest.TestCase):
    def setUp(self):
        self.model = TradeFeeModel()

    def calculate(self, asset, side, amount="1000000"):
        return self.model.calculate(asset_type=asset, side=side, turnover=D(amount))

    def test_stock_buy_has_commission_transfer_but_no_stamp_duty(self):
        fee = self.calculate(AssetType.STOCK, "BUY")
        self.assertEqual((fee.commission, fee.transfer_fee, fee.stamp_duty, fee.handling_fee),
                         (D("100"), D("100"), D("0"), D("0")))
        self.assertEqual(fee.total, D("200"))

    def test_stock_sell_charges_stamp_duty(self):
        fee = self.calculate(AssetType.STOCK, "SELL")
        self.assertEqual((fee.commission, fee.transfer_fee, fee.stamp_duty),
                         (D("100"), D("100"), D("500")))
        self.assertEqual(fee.total, D("700"))

    def test_etf_both_directions_have_commission_and_additive_handling_fee(self):
        for side in ("BUY", "SELL"):
            with self.subTest(side=side):
                fee = self.calculate(AssetType.ETF, side)
                self.assertEqual((fee.commission, fee.handling_fee), (D("50"), D("40")))
                self.assertEqual((fee.stamp_duty, fee.transfer_fee), (D("0"), D("0")))
                self.assertEqual(fee.total, D("90"))

    def test_existing_order_side_enum_is_supported(self):
        self.assertEqual(self.calculate("STOCK", OrderSide.SELL).total, D("700"))

    def test_application_side_must_not_be_used_as_buy_sell(self):
        for side in Side:
            with self.subTest(side=side), self.assertRaises(ValueError):
                self.calculate("STOCK", side)

    def test_no_implicit_five_yuan_minimum(self):
        self.assertEqual(self.calculate("STOCK", "BUY", "1000").total, D("0.20"))
        self.assertEqual(self.calculate("ETF", "BUY", "1000").total, D("0.09"))

    def test_each_fee_is_rounded_to_cents_half_up(self):
        fee = self.calculate("STOCK", "SELL", "50")
        self.assertEqual((fee.commission, fee.transfer_fee, fee.stamp_duty),
                         (D("0.01"), D("0.01"), D("0.03")))
        self.assertEqual(fee.total, D("0.05"))

    def test_zero_turnover_has_zero_cost(self):
        self.assertEqual(self.calculate("ETF", "SELL", "0").total, D("0.00"))

    def test_invalid_amounts_asset_and_side_are_rejected(self):
        for amount in (D("-1"), D("NaN"), D("Infinity")):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.model.calculate(asset_type="STOCK", side="BUY", turnover=amount)
        with self.assertRaises(TypeError):
            self.model.calculate(asset_type="STOCK", side="BUY", turnover=1000.0)
        with self.assertRaises(ValueError):
            self.calculate("OPTION", "BUY")
        with self.assertRaises(ValueError):
            self.calculate("STOCK", "CREATE")

    def test_rates_can_be_configured_and_invalid_rates_fail(self):
        rates = replace(FeeRates(), stock_commission=D("0"))
        model = TradeFeeModel(rates)
        self.assertEqual(model.calculate(asset_type="STOCK", side="BUY", turnover=D("1000000")).total, D("100"))
        for value in (D("-0.1"), D("NaN"), D("Infinity"), D("1")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                FeeRates(stock_commission=value)
        with self.assertRaises(TypeError):
            FeeRates(stock_commission=0.0001)


if __name__ == "__main__":
    unittest.main()
