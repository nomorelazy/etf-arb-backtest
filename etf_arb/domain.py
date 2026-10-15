"""跨模块共享的领域对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    CREATE = "CREATE"
    REDEEM = "REDEEM"


class QuoteStatus(str, Enum):
    NORMAL = "NORMAL"
    SUSPENDED = "SUSPENDED"
    LIMIT_UP = "LIMIT_UP"
    LIMIT_DOWN = "LIMIT_DOWN"
    NO_QUOTE = "NO_QUOTE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class PCFComponent:
    security_code: str
    quantity: Decimal
    security_name: str | None = None
    substitution_flag: str | None = None
    substitution_rate: Decimal | None = None
    creation_premium_rate: Decimal | None = None
    redemption_discount_rate: Decimal | None = None
    substitution_cash_amount: Decimal | None = None
    creation_cash_substitute: Decimal | None = None
    redemption_cash_substitute: Decimal | None = None
    market_code: str | None = None
    is_cash_placeholder: bool = False
    raw_fields: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.security_code:
            raise ValueError("security_code cannot be empty")
        object.__setattr__(self, "security_code", str(self.security_code).strip())
        object.__setattr__(self, "quantity", Decimal(str(self.quantity)))
        if not self.quantity.is_finite() or self.quantity < 0:
            raise ValueError("PCF quantity cannot be negative")


@dataclass(frozen=True)
class PCF:
    """一份申赎单位对应的 PCF；现金调整均按资金账户视角记号，流入为正。"""

    etf_code: str
    effective_date: date
    creation_unit: int
    components: tuple[PCFComponent, ...]
    creation_cash_adjustment: Decimal | None = None
    redemption_cash_adjustment: Decimal | None = None
    exchange: str | None = None
    source_format: str | None = None
    source_path: str | None = None
    version: str | None = None
    symbol: str | None = None
    fund_manager: str | None = None
    security_id_source: str | None = None
    underlying_index_code: str | None = None
    underlying_index_source: str | None = None
    pre_trading_day: date | None = None
    estimated_cash_component: Decimal | None = None
    actual_cash_component: Decimal | None = None
    pre_cash_component: Decimal | None = None
    max_cash_ratio: Decimal | None = None
    nav: Decimal | None = None
    nav_per_cu: Decimal | None = None
    publish_flag: str | None = None
    creation_enabled: bool | None = None
    redemption_enabled: bool | None = None
    creation_redemption_switch: str | None = None
    creation_redemption_mechanism: str | None = None
    creation_limit: Decimal | None = None
    redemption_limit: Decimal | None = None
    creation_limit_per_user: Decimal | None = None
    redemption_limit_per_user: Decimal | None = None
    net_creation_limit: Decimal | None = None
    net_redemption_limit: Decimal | None = None
    net_creation_limit_per_user: Decimal | None = None
    net_redemption_limit_per_user: Decimal | None = None
    record_num: int | None = None
    total_record_num: int | None = None
    dividend_per_cu: Decimal | None = None
    raw_fields: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.etf_code:
            raise ValueError("etf_code cannot be empty")
        if self.creation_unit <= 0:
            raise ValueError("creation_unit must be positive")
        if not self.components:
            raise ValueError("PCF must contain at least one component")
        object.__setattr__(self, "creation_unit", int(self.creation_unit))
        for field_name in (
            "creation_cash_adjustment",
            "redemption_cash_adjustment",
            "estimated_cash_component",
            "actual_cash_component",
            "pre_cash_component",
            "max_cash_ratio",
            "nav",
            "nav_per_cu",
            "creation_limit",
            "redemption_limit",
            "creation_limit_per_user",
            "redemption_limit_per_user",
            "net_creation_limit",
            "net_redemption_limit",
            "net_creation_limit_per_user",
            "net_redemption_limit_per_user",
            "dividend_per_cu",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, Decimal(str(value)))
        if self.effective_date is None:
            raise ValueError("effective_date cannot be None")


@dataclass(frozen=True)
class Quote:
    security_code: str
    ts: datetime
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None
    volume: int | None = None
    status: QuoteStatus = QuoteStatus.UNKNOWN


@dataclass(frozen=True)
class TradeDecision:
    etf_code: str
    ts: datetime
    eligible: bool
    side: Side
    quantity_units: int
    gross_edge: Decimal
    estimated_cost: Decimal
    estimated_net_edge: Decimal
    reasons: tuple[str, ...] = ()
