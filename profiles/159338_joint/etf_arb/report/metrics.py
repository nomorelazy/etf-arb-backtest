"""基于日频净值和已平仓交易计算绩效指标。"""

from __future__ import annotations

from decimal import Decimal, localcontext
from math import isfinite, sqrt
from statistics import stdev
from typing import Iterable

from etf_arb.report.models import DEFAULT_ANNUAL_RISK_FREE_RATE, BacktestSummary, ClosedTrade, EquityPoint


def _annualized_return(
    start: Decimal, end: Decimal, periods: int, periods_per_year: int
) -> Decimal | None:
    if periods <= 0 or start <= 0 or end < 0:
        return None
    with localcontext() as context:
        context.prec = 28
        # Decimal fractional powers preserve report precision without requiring numpy.
        return (end / start) ** (Decimal(periods_per_year) / Decimal(periods)) - Decimal(1)


def calculate_summary(
    equity_curve: Iterable[EquityPoint],
    closed_trades: Iterable[ClosedTrade],
    *,
    starting_cash: Decimal,
    total_costs: Decimal = Decimal("0"),
    peak_cash_usage: Decimal = Decimal("0"),
    filled_orders: int = 0,
    rejected_orders: int = 0,
    failed_applications: int = 0,
    annual_risk_free_rate: float = DEFAULT_ANNUAL_RISK_FREE_RATE,
    periods_per_year: int = 252,
) -> BacktestSummary:
    """计算绩效。

    equity_curve 必须是按交易日排序的账户总权益，首点等于起始资金，末点为结束净值。
    年化默认按 252 个交易日，波动率和 Sharpe 用日简单收益；交易统计基于完整
    平仓交易的净 PnL。收益回撤比按 Calmar 口径定义为年化收益 / 最大回撤绝对值。
    """
    points = sorted(equity_curve, key=lambda point: point.day)
    if starting_cash <= 0:
        raise ValueError("starting_cash must be positive")
    if not isfinite(annual_risk_free_rate) or annual_risk_free_rate <= -1:
        raise ValueError("annual_risk_free_rate must be finite and greater than -100%")
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    if not points:
        raise ValueError("equity_curve must contain at least one point")
    if any(point.equity < 0 for point in points):
        raise ValueError("equity values cannot be negative")
    if len({point.day for point in points}) != len(points):
        raise ValueError("equity_curve cannot contain duplicate dates")

    if points[0].equity != starting_cash:
        raise ValueError("the first equity point must equal starting_cash")

    ending_equity = points[-1].equity
    with localcontext() as context:
        context.prec = 28
        total_return = ending_equity / starting_cash - Decimal(1)

    returns: list[float] = []
    for previous, current in zip(points, points[1:]):
        if previous.equity <= 0:
            raise ValueError("cannot calculate returns after equity reaches zero")
        returns.append(float(current.equity / previous.equity - Decimal(1)))

    peak = points[0].equity
    max_drawdown = Decimal(0)
    for point in points:
        peak = max(peak, point.equity)
        if peak > 0:
            drawdown = point.equity / peak - Decimal(1)
            max_drawdown = min(max_drawdown, drawdown)
    max_drawdown_abs = -max_drawdown

    periods = len(returns)
    annualized_return = _annualized_return(starting_cash, ending_equity, periods, periods_per_year)
    volatility: float | None = None
    sharpe: float | None = None
    if len(returns) >= 2:
        daily_volatility = stdev(returns)
        volatility = daily_volatility * sqrt(periods_per_year)
        if daily_volatility > 0:
            daily_rf = (1 + annual_risk_free_rate) ** (1 / periods_per_year) - 1
            sharpe = (sum(returns) / len(returns) - daily_rf) / daily_volatility * sqrt(periods_per_year)

    trades = list(closed_trades)
    trade_count = len(trades)
    winners = [trade.pnl for trade in trades if trade.pnl > 0]
    losers = [trade.pnl for trade in trades if trade.pnl < 0]
    decisive_trade_count = len(winners) + len(losers)
    win_rate = Decimal(len(winners)) / decisive_trade_count if decisive_trade_count else None
    avg_win = sum(winners, Decimal(0)) / len(winners) if winners else None
    avg_loss_abs = -sum(losers, Decimal(0)) / len(losers) if losers else None
    profit_loss_ratio = (
        avg_win / avg_loss_abs
        if avg_win is not None and avg_loss_abs is not None and avg_loss_abs > 0
        else None
    )
    return_drawdown_ratio = (
        annualized_return / max_drawdown_abs
        if annualized_return is not None and max_drawdown_abs > 0
        else None
    )

    return BacktestSummary(
        starting_cash=starting_cash,
        ending_equity=ending_equity,
        total_return=total_return,
        annualized_return=annualized_return,
        max_drawdown=max_drawdown_abs,
        sharpe_ratio=sharpe,
        win_rate=win_rate,
        profit_loss_ratio=profit_loss_ratio,
        trade_count=trade_count,
        annualized_volatility=volatility,
        return_drawdown_ratio=return_drawdown_ratio,
        total_costs=total_costs,
        peak_cash_usage=peak_cash_usage,
        filled_orders=filled_orders,
        rejected_orders=rejected_orders,
        failed_applications=failed_applications,
        annual_risk_free_rate=annual_risk_free_rate,
    )
