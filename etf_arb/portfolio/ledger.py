"""账户事件账本骨架。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


class LedgerEventType(str, Enum):
    FILL = "FILL"
    CREATE_SUBMITTED = "CREATE_SUBMITTED"
    REDEEM_SUBMITTED = "REDEEM_SUBMITTED"
    APPLICATION_CONFIRMED = "APPLICATION_CONFIRMED"
    APPLICATION_REJECTED = "APPLICATION_REJECTED"
    SECURITIES_AVAILABLE = "SECURITIES_AVAILABLE"
    CASH_SETTLED = "CASH_SETTLED"
    CASH_SUBSTITUTION_SETTLED = "CASH_SUBSTITUTION_SETTLED"


@dataclass(frozen=True)
class LedgerEvent:
    ts: datetime
    event_type: LedgerEventType
    reference: str
    cash_delta: Decimal = Decimal("0")
    security_code: str | None = None
    quantity_delta: int = 0


@dataclass
class PortfolioLedger:
    """按时间追加事实事件；余额投影和交易日结算将在回测阶段实现。"""

    events: list[LedgerEvent] = field(default_factory=list)

    def record(self, event: LedgerEvent) -> None:
        if self.events and event.ts < self.events[-1].ts:
            raise ValueError("Ledger events must be recorded in timestamp order.")
        self.events.append(event)
