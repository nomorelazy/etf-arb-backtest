"""订单、成交和撮合结果对象。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class ExecutionStatus(str, Enum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    NO_FILL = "NO_FILL"


@dataclass(frozen=True)
class Order:
    order_id: str
    security_code: str
    side: OrderSide
    quantity: int
    submitted_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", str(self.order_id).strip())
        object.__setattr__(self, "security_code", str(self.security_code).strip())
        if not self.order_id or not self.security_code:
            raise ValueError("order_id and security_code cannot be empty.")
        try:
            object.__setattr__(self, "side", OrderSide(self.side))
        except ValueError as error:
            raise ValueError("side must be BUY or SELL.") from error
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("Order quantity must be positive.")
        if not isinstance(self.submitted_at, datetime):
            raise ValueError("submitted_at must be a datetime.")


@dataclass(frozen=True)
class Fill:
    order_id: str
    security_code: str
    side: OrderSide
    quantity: int
    price: Decimal
    ts: datetime
    fee: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", str(self.order_id).strip())
        object.__setattr__(self, "security_code", str(self.security_code).strip())
        if not self.order_id or not self.security_code:
            raise ValueError("order_id and security_code cannot be empty.")
        object.__setattr__(self, "price", Decimal(str(self.price)))
        object.__setattr__(self, "fee", Decimal(str(self.fee)))
        try:
            object.__setattr__(self, "side", OrderSide(self.side))
        except ValueError as error:
            raise ValueError("side must be BUY or SELL.") from error
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ValueError("Fill quantity must be positive.")
        if not self.price.is_finite() or self.price <= 0:
            raise ValueError("Fill price must be finite and positive.")
        if not self.fee.is_finite() or self.fee < 0:
            raise ValueError("Fill fee must be finite and non-negative.")
        if not isinstance(self.ts, datetime):
            raise ValueError("Fill timestamp must be a datetime.")


@dataclass(frozen=True)
class ExecutionResult:
    order: Order
    status: ExecutionStatus
    fills: tuple[Fill, ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", ExecutionStatus(self.status))
        object.__setattr__(self, "fills", tuple(self.fills))
        if any(
            fill.order_id != self.order.order_id
            or fill.security_code != self.order.security_code
            or fill.side is not self.order.side
            for fill in self.fills
        ):
            raise ValueError("Execution result contains a fill inconsistent with its order.")
        filled_quantity = sum(fill.quantity for fill in self.fills)
        if filled_quantity > self.order.quantity:
            raise ValueError("Filled quantity cannot exceed order quantity.")
        if self.status is ExecutionStatus.FILLED and filled_quantity != self.order.quantity:
            raise ValueError("A FILLED result must fill the full order quantity.")
        if self.status is ExecutionStatus.PARTIAL and not 0 < filled_quantity < self.order.quantity:
            raise ValueError("A PARTIAL result must contain a partial fill.")
        if self.status in (ExecutionStatus.REJECTED, ExecutionStatus.NO_FILL) and filled_quantity:
            raise ValueError("Rejected and no-fill results cannot contain fills.")
