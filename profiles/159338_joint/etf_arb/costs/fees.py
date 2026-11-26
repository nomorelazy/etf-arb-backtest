"""共享交易税费计算器。

费率按用户指定；按单笔成交、每个费用项目四舍五入到分，不设最低佣金。
只计算费用，不修改资金、不生成信号、不处理申赎申请收费。
"""

from dataclasses import dataclass, fields
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum


ZERO = Decimal("0.00")
CENT = Decimal("0.01")


class AssetType(str, Enum):
    STOCK = "STOCK"
    ETF = "ETF"


@dataclass(frozen=True)
class FeeRates:
    stock_commission: Decimal = Decimal("0.0001")       # 万1，双向
    etf_commission: Decimal = Decimal("0.00005")        # 万0.5，双向
    stock_stamp_duty: Decimal = Decimal("0.0005")       # 万5，仅卖出
    stock_transfer_fee: Decimal = Decimal("0.0001")     # 万1，双向
    etf_handling_fee: Decimal = Decimal("0.00004")      # 万0.4，双向

    def __post_init__(self):
        for item in fields(self):
            rate = getattr(self, item.name)
            if not isinstance(rate, Decimal):
                raise TypeError(f"{item.name} must be Decimal")
            if not rate.is_finite() or not ZERO <= rate < 1:
                raise ValueError(f"Invalid rate: {item.name}={rate}")

    @classmethod
    def zero(cls):
        """Explicitly reproduce historical zero-fee research runs."""
        return cls(**{item.name: ZERO for item in fields(cls)})

    @classmethod
    def from_dict(cls, values):
        if not isinstance(values, dict) or set(values) - {item.name for item in fields(cls)}:
            raise ValueError("Unknown or invalid transaction fee settings")
        if any(isinstance(value, (bool, float)) for value in values.values()):
            raise TypeError("Fee rates require decimal strings, Decimal, or integers")
        return cls(**{name: Decimal(str(value)) for name, value in values.items()})


@dataclass(frozen=True)
class FeeBreakdown:
    commission: Decimal = ZERO
    stamp_duty: Decimal = ZERO
    transfer_fee: Decimal = ZERO
    handling_fee: Decimal = ZERO

    def __post_init__(self):
        for item in fields(self):
            amount = getattr(self, item.name)
            if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
                raise ValueError(f"Invalid fee amount: {item.name}")

    @property
    def total(self) -> Decimal:
        return (self.commission + self.stamp_duty
                + self.transfer_fee + self.handling_fee)


class TradeFeeModel:
    def __init__(self, rates: FeeRates | None = None):
        self.rates = FeeRates() if rates is None else rates
        if not isinstance(self.rates, FeeRates):
            raise TypeError("rates must be FeeRates")

    def calculate(
        self,
        *,
        asset_type: AssetType | str,
        side: str,
        turnover: Decimal,
    ) -> FeeBreakdown:
        """turnover = 实际成交价 × 实际成交股数/份数。

        side 使用 BUY/SELL，兼容现有执行模块的 OrderSide；
        不能传入申赎方向 CREATE/REDEEM。
        """
        asset_type = AssetType(asset_type)
        if side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if not isinstance(turnover, Decimal):
            raise TypeError("turnover must be Decimal")
        if not turnover.is_finite() or turnover < 0:
            raise ValueError("turnover must be finite and non-negative")

        def charge(rate: Decimal) -> Decimal:
            return (turnover * rate).quantize(CENT, rounding=ROUND_HALF_UP)

        if asset_type is AssetType.STOCK:
            return FeeBreakdown(
                commission=charge(self.rates.stock_commission),
                stamp_duty=(charge(self.rates.stock_stamp_duty)
                            if side == "SELL" else ZERO),
                transfer_fee=charge(self.rates.stock_transfer_fee),
            )

        return FeeBreakdown(
            commission=charge(self.rates.etf_commission),
            handling_fee=charge(self.rates.etf_handling_fee),
        )
