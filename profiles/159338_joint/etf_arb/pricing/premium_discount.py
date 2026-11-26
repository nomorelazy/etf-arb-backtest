"""同一时点、每份 ETF 价格相对每份 IOPV 的折溢价。

价差 = ETF 每份价格 - 每份 IOPV。
折溢价率 = 价差 / 每份 IOPV；正数为溢价，负数为折价。
calculate_premium_discount_rate 返回百分数数值，即上述比例乘以 100。

调用方负责传入同一 ETF、同一估值时点、相同币种的每份数值。
本模块接收已经计算好的 IOPV，不包含手续费或成交模拟。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from etf_arb.pricing.errors import PricingDataError


@dataclass(frozen=True)
class PremiumDiscountResult:
    """每份价差与折溢价率；rate=Decimal('0.01') 表示溢价 1%。"""

    etf_price: Decimal
    iopv: Decimal
    price_difference: Decimal
    premium_discount_rate: Decimal


def calculate_premium_discount(
    etf_price: Decimal,
    iopv: Decimal,
) -> PremiumDiscountResult:
    """用每份 ETF 价格和每份 IOPV 计算价差与折溢价率。

    两个参数均要求是有限且大于零的 Decimal。
    若 IOPV 输入是一整个申赎单位的总价值，应先除以对应 ETF 份额。
    ETF 价格可由调用方选取分钟收盘价或指定盘口价，需保持口径一致。

    示例：
        >>> result = calculate_premium_discount(Decimal('1.02'), Decimal('1.00'))
        >>> result.price_difference
        Decimal('0.02')
        >>> f'{result.premium_discount_rate:.2%}'
        '2.00%'
    """
    for name, value in (("etf_price", etf_price), ("iopv", iopv)):
        if not isinstance(value, Decimal):
            raise TypeError(f"{name} must be a Decimal.")
        if not value.is_finite() or value <= 0:
            raise PricingDataError(f"{name} must be finite and positive.")

    price_difference = etf_price - iopv
    return PremiumDiscountResult(
        etf_price=etf_price,
        iopv=iopv,
        price_difference=price_difference,
        premium_discount_rate=price_difference / iopv,
    )


def calculate_premium_discount_rate(
    etf_price: Decimal,
    iopv: Decimal,
) -> Decimal:
    """计算百分数口径的折溢价率：(ETF 价格 - IOPV) / IOPV * 100。

    接口输入：同一 ETF、同一时点、同一币种的每份 ETF 价格与每份 IOPV，
    两者均须为有限且大于零的 Decimal。行情读取、时间对齐和 IOPV 计算
    由调用方完成；本函数只负责折溢价公式。

    接口输出：Decimal 百分数数值，2 表示溢价 2%，-2 表示折价 2%。
    展示时使用 f'{rate:.2f}%'，不要再乘 100 或使用百分比格式化。

    示例：
        >>> rate = calculate_premium_discount_rate(Decimal('1.02'), Decimal('1.00'))
        >>> f'{rate:.2f}%'
        '2.00%'
    """
    result = calculate_premium_discount(etf_price, iopv)
    return result.premium_discount_rate * Decimal("100")


__all__ = [
    "PremiumDiscountResult",
    "calculate_premium_discount",
    "calculate_premium_discount_rate",
]
