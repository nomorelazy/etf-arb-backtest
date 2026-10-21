"""159338 盘中 IOPV 估值。

本模块只负责 T 日盘中 IOPV，不处理 T+1/T+2 现金替代结算。

核心公式：

    IOPV_t = (
        A
        + sum(Q_i * P_i,t for allowed securities)
        + sum(Q_j * P_j,t for forbidden securities)
        + F_t
        + E
    ) / U

其中：
- A: 必须现金替代证券的固定替代金额总额；
- E: PCF 预估现金部分；
- F_t: 碎股现金替代的 T 日盘中即时估值；
- U: 最小申购赎回单位对应的基金份额。

重要处理顺序：
1. 先剔除“申赎现金”虚拟行；
2. 再读取 SubstituteFlag；
3. SubstituteFlag=2 时直接读取 CreationCashSubstitute，
   不检查 ComponentShare；
4. 只有 SubstituteFlag=0/1 才处理 ComponentShare；
5. ComponentShare 的碎股（不足 100 股）按现金替代处理，
   但盘中仍使用当前最新成交价估值。

本项目分钟行情使用北京时间的 naive datetime，不接受带时区的 datetime。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Callable, Mapping

from etf_arb.domain import PCF, PCFComponent, Quote
from etf_arb.pricing.errors import PricingDataError
from etf_arb.rules import is_continuous_auction_time


A_SHARE_LOT_SIZE = Decimal("100")

# quote.ts 与估值时刻相差超过一个完整分钟 bar，才认为价格陈旧。
IOPV_STALE_AFTER = timedelta(minutes=1)


class SubstitutionKind(str, Enum):
    """PCF 现金替代标志。"""

    FORBIDDEN = "FORBIDDEN"   # 0：禁止现金替代
    ALLOWED = "ALLOWED"       # 1：可以现金替代
    MANDATORY = "MANDATORY"   # 2：必须现金替代


@dataclass(frozen=True)
class IOPVValuation:
    """159338 单一时刻的 IOPV 估值结果。"""

    etf_code: str
    ts: datetime
    iopv: Decimal

    # IOPV 公式各组成部分
    mandatory_cash: Decimal
    allowed_security_value: Decimal
    forbidden_security_value: Decimal
    fractional_cash_value: Decimal
    estimated_cash: Decimal

    # 分母
    creation_unit: int

    # 覆盖率分子/分母所需的原始计数
    total_component_count: int
    mandatory_component_count: int
    priced_component_count: int

    # 价格质量
    stale_quote_count: int
    max_quote_age: timedelta | None

    @property
    def numerator(self) -> Decimal:
        """IOPV 公式分子。"""
        return (
            self.mandatory_cash
            + self.allowed_security_value
            + self.forbidden_security_value
            + self.fractional_cash_value
            + self.estimated_cash
        )

    @property
    def price_required_component_count(self) -> int:
        """需要行情价格的证券行数。"""
        return (
            self.total_component_count
            - self.mandatory_component_count
        )

    @property
    def coverage(self) -> Decimal | None:
        """行情覆盖率：priced / (total - mandatory)。

        分母为 0 时返回 None，不伪造 100%。
        """
        denominator = self.price_required_component_count
        if denominator == 0:
            return None

        return (
            Decimal(self.priced_component_count)
            / Decimal(denominator)
        )


def _normalize_substitution_flag(
    flag: str | None,
) -> SubstitutionKind:
    """严格解析现金替代标志。

    深市/沪市原始数据可能使用数字或中文语义。
    未识别的标志直接报错，禁止静默归类。
    """

    if flag is None:
        raise PricingDataError(
            "PCF component has no substitution flag."
        )

    value = str(flag).strip()

    if value in {"0", "禁止", "禁止现金替代"}:
        return SubstitutionKind.FORBIDDEN

    if value in {"1", "允许", "允许现金替代"}:
        return SubstitutionKind.ALLOWED

    if value in {"2", "必须", "必须现金替代"}:
        return SubstitutionKind.MANDATORY

    if value.startswith("0-"):
        return SubstitutionKind.FORBIDDEN

    if value.startswith("1-"):
        return SubstitutionKind.ALLOWED

    if value.startswith("2-"):
        return SubstitutionKind.MANDATORY

    raise PricingDataError(
        f"Unknown substitution flag: {flag!r}"
    )


def _component_quantity(
    component: PCFComponent,
) -> Decimal:
    """读取 0/1 类 ComponentShare。

    只能在 SubstituteFlag=0/1 之后调用。
    """

    quantity = component.quantity

    if quantity != quantity.to_integral_value():
        raise PricingDataError(
            "Non-integer ComponentShare for "
            f"{component.security_code}: {quantity}"
        )

    if quantity < 0:
        raise PricingDataError(
            "Negative ComponentShare for "
            f"{component.security_code}: {quantity}"
        )

    return quantity


def _split_lot_quantity(
    quantity: Decimal,
) -> tuple[Decimal, Decimal]:
    """拆分整手与碎股。

    例如：
        1250 -> 1200 + 50
         100 ->  100 + 0
          50 ->    0 + 50
    """

    lot_quantity = (
        quantity // A_SHARE_LOT_SIZE
    ) * A_SHARE_LOT_SIZE

    fractional_quantity = quantity - lot_quantity

    return lot_quantity, fractional_quantity


def _mandatory_cash_amount(
    component: PCFComponent,
) -> Decimal:
    """读取 SubstituteFlag=2 的固定现金替代金额。

    关键：不读取 ComponentShare。
    """

    amount = component.creation_cash_substitute

    if amount is None:
        raise PricingDataError(
            "Mandatory cash-substitution component "
            f"{component.security_code} is missing "
            "CreationCashSubstitute."
        )

    amount = Decimal(str(amount))

    if not amount.is_finite() or amount < 0:
        raise PricingDataError(
            "Invalid CreationCashSubstitute for "
            f"{component.security_code}: {amount}"
        )

    return amount


def _latest_price(
    component: PCFComponent,
    quote: Quote | None,
    at: datetime,
) -> Decimal:
    """获取截至 at 的最近有效成交价。

    不允许使用未来行情。
    """

    if quote is None:
        raise PricingDataError(
            "Missing quote for PCF component "
            f"{component.security_code}."
        )

    if quote.ts.tzinfo is not None:
        raise PricingDataError(
            "Quote timestamp must be timezone-naive "
            f"Beijing local time: {component.security_code}"
        )

    if quote.ts > at:
        raise PricingDataError(
            "Future quote detected for "
            f"{component.security_code}: "
            f"quote={quote.ts}, valuation={at}"
        )

    if quote.last is None:
        raise PricingDataError(
            "No last price available for "
            f"{component.security_code}."
        )

    price = Decimal(str(quote.last))

    if not price.is_finite() or price <= 0:
        raise PricingDataError(
            "Invalid last price for "
            f"{component.security_code}: {price}"
        )

    return price


def calculate_iopv_159338(
    pcf: PCF,
    quotes: Mapping[str, Quote],
    at: datetime,
    *,
    session_validator: Callable[[datetime], bool] = is_continuous_auction_time,
) -> IOPVValuation:
    """计算 159338 T 日指定时刻的盘中 IOPV。

    只使用：
        - T 日 PCF；
        - T 日 at 时刻及之前的最新行情。

    不使用任何 T+1/T+2 价格。模拟器可显式传入用户交易时段校验器；
    日期、行情时间及其他数据校验保持不变。
    """

    # ============================================================
    # 1. ETF 校验
    # ============================================================

    if pcf.etf_code != "159338":
        raise ValueError(
            "This IOPV model is currently dedicated to 159338, "
            f"got {pcf.etf_code!r}"
        )

    # ============================================================
    # 2. 时间必须是北京时间 naive datetime
    # ============================================================

    if at.tzinfo is not None:
        raise ValueError(
            "IOPV timestamp must be timezone-naive "
            "Beijing local time."
        )

    # ============================================================
    # 3. PCF 必须严格对应 T 日
    #
    # 不提供任何绕过日期校验的开关。
    # ============================================================

    if pcf.effective_date != at.date():
        raise ValueError(
            "PCF effective date does not match valuation date: "
            f"pcf={pcf.effective_date}, "
            f"valuation={at.date()}"
        )

    # ============================================================
    # 4. 只允许连续竞价时间
    # ============================================================

    if not session_validator(at):
        raise ValueError(
            f"{at} is outside the configured valuation sessions."
        )

    # ============================================================
    # 5. PCF 基础字段
    # ============================================================

    if pcf.creation_unit <= 0:
        raise PricingDataError(
            f"Invalid creation unit: {pcf.creation_unit}"
        )

    if pcf.estimated_cash_component is None:
        raise PricingDataError(
            "159338 PCF is missing estimated cash component."
        )

    estimated_cash = Decimal(
        str(pcf.estimated_cash_component)
    )

    if not estimated_cash.is_finite():
        raise PricingDataError(
            "Estimated cash component must be finite."
        )

    # ============================================================
    # 6. IOPV 分子各组成部分
    # ============================================================

    mandatory_cash = Decimal("0")
    allowed_security_value = Decimal("0")
    forbidden_security_value = Decimal("0")
    fractional_cash_value = Decimal("0")

    # ============================================================
    # 7. 数据质量计数
    #
    # total:
    #     所有真实组合证券 Component
    #
    # mandatory:
    #     SubstituteFlag=2，不需要行情
    #
    # priced:
    #     实际读取到有效行情的 0/1 Component
    #
    # coverage:
    #     priced / (total - mandatory)
    # ============================================================

    total_component_count = 0
    mandatory_component_count = 0
    priced_component_count = 0

    stale_quote_count = 0
    max_quote_age: timedelta | None = None

    # ============================================================
    # 8. 遍历 PCF
    # ============================================================

    for component in pcf.components:

        # --------------------------------------------------------
        # 8.1 “申赎现金”虚拟行
        #
        # 它不是组合成份证券。
        # 即使它的现金替代标志是“必须”，也必须在这里排除。
        # --------------------------------------------------------

        if component.is_cash_placeholder:
            continue

        total_component_count += 1

        # --------------------------------------------------------
        # 8.2 先分类，再处理数量
        #
        # 这是 PCF 解析/估值的关键顺序。
        # --------------------------------------------------------

        kind = _normalize_substitution_flag(
            component.substitution_flag
        )

        # --------------------------------------------------------
        # 8.3 必须现金替代
        #
        # SubstituteFlag=2 时：
        #
        #   ComponentShare 可以为 0；
        #   不需要行情；
        #   直接读取 CreationCashSubstitute。
        #
        # 绝不能在这里之前判断 quantity == 0。
        # --------------------------------------------------------

        if kind is SubstitutionKind.MANDATORY:
            mandatory_component_count += 1

            mandatory_cash += _mandatory_cash_amount(
                component
            )

            continue

        # --------------------------------------------------------
        # 8.4 只有 0/1 才读取 ComponentShare
        # --------------------------------------------------------

        quantity = _component_quantity(component)

        # 防御性检查。
        # 当前 159338 数据中 flag=0/1 + quantity=0 未观察到，
        # 它不是主要业务逻辑。
        if quantity == 0:
            continue

        # --------------------------------------------------------
        # 8.5 获取截至 at 的最近有效成交价
        # --------------------------------------------------------

        quote = quotes.get(component.security_code)

        price = _latest_price(
            component,
            quote,
            at,
        )

        priced_component_count += 1

        # --------------------------------------------------------
        # 8.6 价格年龄
        #
        # 不再使用 quote.ts < at 作为 stale。
        # 那只是在统计“当前这一分钟有没有新成交”。
        #
        # 这里要求超过一个完整分钟 bar 才算 stale。
        # --------------------------------------------------------

        assert quote is not None

        quote_age = at - quote.ts

        if max_quote_age is None or quote_age > max_quote_age:
            max_quote_age = quote_age

        if quote_age > IOPV_STALE_AFTER:
            stale_quote_count += 1

        # --------------------------------------------------------
        # 8.7 拆分整手和碎股
        # --------------------------------------------------------

        lot_quantity, fractional_quantity = (
            _split_lot_quantity(quantity)
        )

        lot_value = lot_quantity * price

        # --------------------------------------------------------
        # 8.8 整手股票价值
        # --------------------------------------------------------

        if kind is SubstitutionKind.ALLOWED:
            allowed_security_value += lot_value
        else:
            # MANDATORY 已经在上面 continue，
            # 因此这里剩余的合法类型就是 FORBIDDEN。
            forbidden_security_value += lot_value

        # --------------------------------------------------------
        # 8.9 碎股现金替代
        #
        # 碎股不足 100 股，按照现金替代处理。
        #
        # 但这是 T 日盘中 IOPV：
        # 仍然使用当前最新成交价，不使用 T+2 收盘价。
        # --------------------------------------------------------

        if fractional_quantity > 0:
            fractional_cash_value += (
                fractional_quantity * price
            )

    # ============================================================
    # 9. IOPV 分子
    # ============================================================

    numerator = (
        mandatory_cash
        + allowed_security_value
        + forbidden_security_value
        + fractional_cash_value
        + estimated_cash
    )

    # ============================================================
    # 10. 除以最小申购赎回单位
    # ============================================================

    iopv = numerator / Decimal(pcf.creation_unit)

    return IOPVValuation(
        etf_code=pcf.etf_code,
        ts=at,
        iopv=iopv,
        mandatory_cash=mandatory_cash,
        allowed_security_value=allowed_security_value,
        forbidden_security_value=forbidden_security_value,
        fractional_cash_value=fractional_cash_value,
        estimated_cash=estimated_cash,
        creation_unit=pcf.creation_unit,
        total_component_count=total_component_count,
        mandatory_component_count=mandatory_component_count,
        priced_component_count=priced_component_count,
        stale_quote_count=stale_quote_count,
        max_quote_age=max_quote_age,
    )


__all__ = [
    "A_SHARE_LOT_SIZE",
    "IOPV_STALE_AFTER",
    "IOPVValuation",
    "SubstitutionKind",
    "calculate_iopv_159338",
]
