"""报告输入与指标数据结构。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class EquityPoint:
    """一个估值点的账户总权益（现金 + 按规则标记的持仓市值）。"""

    day: date
    equity: Decimal


@dataclass(frozen=True)
class ClosedTrade:
    """一笔完整套利交易的净 PnL；未平仓交易不应计入胜率统计。"""

    trade_id: str
    pnl: Decimal


@dataclass(frozen=True)
class BacktestSummary:
    starting_cash: Decimal
    ending_equity: Decimal
    total_return: Decimal
    annualized_return: Decimal | None
    max_drawdown: Decimal
    sharpe_ratio: float | None
    win_rate: Decimal | None
    profit_loss_ratio: Decimal | None
    trade_count: int
    annualized_volatility: float | None
    return_drawdown_ratio: Decimal | None
    total_costs: Decimal = Decimal("0")
    peak_cash_usage: Decimal = Decimal("0")
    filled_orders: int = 0
    rejected_orders: int = 0
    failed_applications: int = 0
