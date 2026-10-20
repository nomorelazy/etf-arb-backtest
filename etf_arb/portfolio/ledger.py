"""账户事件账本与套利生命周期记录。

当前版本提供事件流水、套利发起/结算登记和按发起日期汇总净盈亏。
余额投影、持仓可用性与交易日结算逻辑将在后续阶段实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from enum import Enum

from etf_arb.report.models import ClosedTrade


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


@dataclass(frozen=True)
class ArbitrageTrade:
    """套利生命周期记录；pnl 只保存最终结算后的净盈亏。"""

    trade_id: str
    opened_at: datetime
    settled_at: datetime | None = None
    pnl: Decimal | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.trade_id, str) or not self.trade_id.strip():
            raise ValueError("trade_id 必须是非空字符串。")

        if not isinstance(self.opened_at, datetime):
            raise TypeError("opened_at 必须是 datetime。")
        if self.opened_at.tzinfo is not None:
            raise ValueError("opened_at 必须是无时区的北京时间。")

        # 结算时间和最终盈亏必须同时提供，或同时为空。
        if (self.settled_at is None) != (self.pnl is None):
            raise ValueError("settled_at 和 pnl 必须同时提供或同时为空。")

        if self.settled_at is not None:
            if not isinstance(self.settled_at, datetime):
                raise TypeError("settled_at 必须是 datetime。")
            if self.settled_at.tzinfo is not None:
                raise ValueError("settled_at 必须是无时区的北京时间。")
            if self.settled_at < self.opened_at:
                raise ValueError("结算时间不能早于发起时间。")

        if self.pnl is not None:
            if not isinstance(self.pnl, Decimal):
                raise TypeError("pnl 必须是 Decimal。")
            if not self.pnl.is_finite():
                raise ValueError("pnl 必须是有限数值。")

    @property
    def is_settled(self) -> bool:
        return self.settled_at is not None and self.pnl is not None


@dataclass(frozen=True)
class PeriodPnlSummary:
    """按发起日期筛选的交易统计。"""

    opened_count: int
    settled_count: int
    pending_count: int
    settled_net_pnl: Decimal

    @property
    def final_net_pnl(self) -> Decimal | None:
        """全部完成后才有最终总额；有未完成交易时返回 None。"""
        if self.pending_count:
            return None
        return self.settled_net_pnl


@dataclass
class PortfolioLedger:
    """账户事件流水与套利生命周期登记。

    当前事件流水和交易生命周期分别保存在内存中；本版本尚未实现
    从 LedgerEvent 重建 ArbitrageTrade 状态的投影器。
    """

    events: list[LedgerEvent] = field(default_factory=list)

    # 明确新增交易集合；同一个 trade_id 只能登记一次。
    _trades: dict[str, ArbitrageTrade] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def record(self, event: LedgerEvent) -> None:
        """保留现有事件流水接口。"""
        if self.events and event.ts < self.events[-1].ts:
            raise ValueError(
                "Ledger events must be recorded in timestamp order."
            )
        self.events.append(event)

    def open_trade(
        self,
        *,
        trade_id: str,
        opened_at: datetime,
    ) -> None:
        """套利发起时立即登记，包括之后可能跨日未完成的交易。"""
        trade = ArbitrageTrade(
            trade_id=trade_id,
            opened_at=opened_at,
        )
        if trade.trade_id in self._trades:
            raise ValueError(f"交易已登记：{trade.trade_id}")
        self._trades[trade.trade_id] = trade

    def settle_trade(
        self,
        *,
        trade_id: str,
        settled_at: datetime,
        pnl: Decimal,
    ) -> None:
        """全部交易腿及最终现金结算完成后，由执行/结算模块调用。"""
        if settled_at is None or pnl is None:
            raise ValueError("必须提供最终结算时间和净盈亏。")

        if trade_id not in self._trades:
            raise ValueError(f"未找到套利交易：{trade_id}")

        trade = self._trades[trade_id]
        if trade.is_settled:
            raise ValueError(f"交易已完成结算：{trade_id}")

        # replace 会重新执行 ArbitrageTrade 的字段校验。
        self._trades[trade.trade_id] = replace(
            trade,
            settled_at=settled_at,
            pnl=pnl,
        )

    @property
    def trades(self) -> tuple[ArbitrageTrade, ...]:
        return tuple(
            sorted(
                self._trades.values(),
                key=lambda trade: (trade.opened_at, trade.trade_id),
            )
        )

    def summarize_pnl(
        self,
        start_date: date,
        end_date: date,
    ) -> PeriodPnlSummary:
        """起止日期均包含；按发起日筛选，不限制结算日。"""
        if type(start_date) is not date or type(end_date) is not date:
            raise TypeError("起止日期必须是 date。")
        if start_date > end_date:
            raise ValueError("开始日期不能晚于结束日期。")

        selected = [
            trade
            for trade in self._trades.values()
            if start_date <= trade.opened_at.date() <= end_date
        ]
        settled = [trade for trade in selected if trade.is_settled]

        return PeriodPnlSummary(
            opened_count=len(selected),
            settled_count=len(settled),
            pending_count=len(selected) - len(settled),
            settled_net_pnl=sum(
                (
                    trade.pnl
                    for trade in settled
                    if trade.pnl is not None
                ),
                Decimal("0"),
            ),
        )

    def total_net_pnl(
        self,
        start_date: date,
        end_date: date,
    ) -> Decimal:
        """只返回该期间发起交易的最终总盈亏；未全部完成时报错。"""
        summary = self.summarize_pnl(start_date, end_date)
        result = summary.final_net_pnl
        if result is None:
            raise ValueError(
                f"区间内仍有 {summary.pending_count} 笔套利未完成，"
                "暂不能计算最终盈亏总额。"
            )
        return result

    def to_closed_trades(self) -> tuple[ClosedTrade, ...]:
        """适配现有报告接口，不假设 ClosedTrade 有日期字段。"""
        return tuple(
            ClosedTrade(trade_id=trade.trade_id, pnl=trade.pnl)
            for trade in self.trades
            if trade.is_settled and trade.pnl is not None
        )
