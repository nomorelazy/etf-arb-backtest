"""PNG 可视化与 CSV 指标报告。"""

from __future__ import annotations

import csv
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from html import escape
from typing import Sequence

from etf_arb.report.metrics import calculate_summary
from etf_arb.report.models import BacktestSummary, ClosedTrade, EquityPoint


def _format_metric(value: object) -> str:
    if value is None:
        return "N/A"
    if hasattr(value, "as_tuple"):
        return f"{float(value):.4f}"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


_PERCENT_METRICS = {
    "total_return",
    "annualized_return",
    "max_drawdown",
    "win_rate",
    "annualized_volatility",
}
_CURRENCY_METRICS = {"starting_cash", "ending_equity", "total_costs", "peak_cash_usage"}
_COUNT_METRICS = {"trade_count", "filled_orders", "rejected_orders", "failed_applications"}


def _format_percent(value: object, *, absolute: bool = False) -> str:
    if value is None:
        return "N/A"
    number = float(value)
    if absolute:
        number = abs(number)
    return f"{number:.2%}"


def save_metrics_csv(summary: BacktestSummary, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(("metric", "value", "unit"))
        for field in fields(summary):
            value = getattr(summary, field.name)
            if field.name in _PERCENT_METRICS and value is not None:
                writer.writerow((field.name, f"{float(value) * 100:.8f}", "%"))
            else:
                unit = (
                    "currency" if field.name in _CURRENCY_METRICS
                    else "count" if field.name in _COUNT_METRICS
                    else "ratio" if field.name in {"sharpe_ratio", "profit_loss_ratio", "return_drawdown_ratio"}
                    else ""
                )
                writer.writerow((field.name, _format_metric(value), unit))
    return target


def save_equity_csv(curve: Sequence[EquityPoint], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.writer(file)
        writer.writerow(("date", "equity", "pnl", "drawdown"))
        if not curve:
            return target
        start = curve[0].equity
        peak = start
        for point in sorted(curve, key=lambda item: item.day):
            peak = max(peak, point.equity)
            drawdown = point.equity / peak - 1 if peak else 0
            writer.writerow((point.day.isoformat(), str(point.equity), str(point.equity - start), str(drawdown)))
    return target


def save_report(
    equity_curve: Sequence[EquityPoint],
    closed_trades: Sequence[ClosedTrade],
    output_dir: str | Path,
    *,
    starting_cash: Decimal | str | int,
    total_costs: Decimal | str | int = Decimal("0"),
    annual_risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
    peak_cash_usage: Decimal = Decimal("0"),
    filled_orders: int = 0,
    rejected_orders: int = 0,
    failed_applications: int = 0,
    title: str = "ETF Arbitrage Backtest",
    trade_rows: Sequence[dict] | None = None,
    financial_summary: dict | None = None,
) -> BacktestSummary:
    """输出 metrics.csv、equity.csv 和 pnl_report.png。"""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    points = sorted(equity_curve, key=lambda item: item.day)
    opening_equity = Decimal(str(starting_cash))
    summary = calculate_summary(
        points,
        closed_trades,
        starting_cash=opening_equity,
        total_costs=Decimal(str(total_costs)),
        annual_risk_free_rate=annual_risk_free_rate,
        periods_per_year=periods_per_year,
        peak_cash_usage=peak_cash_usage,
        filled_orders=filled_orders,
        rejected_orders=rejected_orders,
        failed_applications=failed_applications,
    )
    save_metrics_csv(summary, output / "metrics.csv")
    save_equity_csv(points, output / "equity.csv")

    dates = [point.day for point in points]
    equities = [float(point.equity) for point in points]
    start = float(opening_equity)
    pnl = [value - start for value in equities]
    peaks: list[float] = []
    peak = start
    for value in equities:
        peak = max(peak, value)
        peaks.append(peak)
    drawdowns = [(value / high - 1) if high else 0.0 for value, high in zip(equities, peaks)]

    fig, (ax_pnl, ax_dd) = plt.subplots(
        2, 1, figsize=(12, 8), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    ax_pnl.plot(dates, pnl, color="#1769aa", linewidth=1.8, label="Cumulative PnL")
    ax_pnl.axhline(0, color="#777777", linewidth=0.8)
    ax_pnl.set_ylabel("PnL")
    ax_pnl.grid(True, alpha=0.25)
    ax_pnl.legend(loc="upper left")

    ax_dd.fill_between(dates, drawdowns, 0, color="#d9534f", alpha=0.35)
    ax_dd.plot(dates, drawdowns, color="#b52b27", linewidth=1.0)
    ax_dd.set_ylabel("Drawdown")
    ax_dd.yaxis.set_major_formatter(plt.FuncFormatter(lambda value, _: f"{value:.2%}"))
    ax_dd.grid(True, alpha=0.25)
    ax_dd.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax_dd.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()

    metric_lines = [
        f"Annualized: {_format_percent(summary.annualized_return)}",
        f"Max DD: {_format_percent(summary.max_drawdown, absolute=True)}",
        f"Sharpe: {_format_metric(summary.sharpe_ratio)}",
        f"Win rate: {_format_percent(summary.win_rate)}",
        f"P/L ratio: {_format_metric(summary.profit_loss_ratio)}",
        f"Trades: {summary.trade_count}",
        f"Volatility: {_format_percent(summary.annualized_volatility)}",
        f"Return / DD: {_format_metric(summary.return_drawdown_ratio)}",
    ]
    fig.suptitle(title + "\n" + "  |  ".join(metric_lines), fontsize=9, y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.91))
    chart_path = output / "pnl_report.png"
    fig.savefig(chart_path, dpi=160, bbox_inches="tight")
    plt.close(fig)
    _save_html(summary, output / "report.html", chart_path.name,
               trade_rows=trade_rows, financial_summary=financial_summary)
    return summary


def _save_html(summary: BacktestSummary, path: Path, chart_name: str, *, trade_rows=None, financial_summary=None) -> None:
    labels = {
        "total_return": "区间收益率",
        "annualized_return": "年化收益率",
        "max_drawdown": "最大回撤",
        "sharpe_ratio": "夏普比率",
        "win_rate": "胜率（平局不计入分母）",
        "profit_loss_ratio": "盈亏比（平均盈利 / 平均亏损）",
        "trade_count": "已平仓交易次数",
        "annualized_volatility": "年化波动率",
        "return_drawdown_ratio": "收益回撤比（年化收益 / 最大回撤）",
    }
    values = {
        "total_return": _format_percent(summary.total_return),
        "annualized_return": _format_percent(summary.annualized_return),
        "max_drawdown": _format_percent(summary.max_drawdown),
        "sharpe_ratio": _format_metric(summary.sharpe_ratio),
        "win_rate": _format_percent(summary.win_rate),
        "profit_loss_ratio": _format_metric(summary.profit_loss_ratio),
        "trade_count": str(summary.trade_count),
        "annualized_volatility": _format_percent(summary.annualized_volatility),
        "return_drawdown_ratio": _format_metric(summary.return_drawdown_ratio),
    }
    rows = "\n".join(
        f"<tr><th>{labels[key]}</th><td>{values[key]}</td></tr>" for key in labels
    )
    details = ""
    if financial_summary is not None:
        final = financial_summary['final_net_pnl']
        final_text = "待全部结清后确定" if final is None else f"{Decimal(final):,.2f} 元"
        details += (f"<h2>发起期间的最终盈亏</h2><p>{escape(final_text)}；"
                    f"已结清 {financial_summary['settled_count']} 笔，待完成 {financial_summary['pending_count']} 笔。</p>"
                    f"<p>本轮折溢价门槛：{Decimal(financial_summary['premium_threshold']):.4%}。"
                    "close 简化成交；定价完成与现金到账分别记录，待交收不计作最终结清。</p>")
        if financial_summary.get('frequency_anchor') == 'application_submitted_at':
            details += (f"<p>交易节奏：以上一笔实际提交申赎时间起算，满 {financial_summary['min_trade_interval_minutes']} 分钟后，"
                        "有合格信号且可用现金足够即可发起下一笔，允许多笔未结清套利并行。"
                        f"本轮未结清套利数量峰值：{financial_summary['peak_open_cycles']} 笔。</p>")
        if 'fee_breakdown' in financial_summary:
            details += (f"<h2>交易税费</h2><p>成交税费合计 {Decimal(financial_summary['fee']):,.2f} 元；"
                        f"其中投资者账户扣款 {Decimal(financial_summary['investor_fees']):,.2f} 元，"
                        f"代买卖费用 {Decimal(financial_summary['manager_fees']):,.2f} 元计入现金替代退补款。</p>")
            from etf_arb.report.ledger import _table
            names = {'commission': '佣金', 'stamp_duty': '印花税', 'transfer_fee': '过户费', 'handling_fee': '经手费'}
            details += _table([{'费用项目': label, '金额（元）': financial_summary['fee_breakdown'][key]}
                               for key, label in names.items()])
    if trade_rows is not None:
        from etf_arb.report.ledger import _table
        details += "<h2>已结清套利六列账本（按发起时间）</h2>" + _table(trade_rows)
        quality = ('<a href="../quality/summary.json">数据缺失检查</a>'
                   if (path.parent.parent / 'quality/summary.json').is_file()
                   else '本轮未执行缺失检查')
        details += ('<p><a href="ledger.html">逐笔全部现金、证券、成交及结算账目（可打印）</a> | '
                    + quality + ' | '
                    '<a href="../assumptions.json">本轮配置与假设</a></p>')
    document = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ETF 申赎套利回测报告</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 1100px; margin: 32px auto; padding: 0 20px; color: #20252b; }}
table {{ border-collapse: collapse; min-width: 420px; margin: 20px 0; }}
th, td {{ border-bottom: 1px solid #ddd; padding: 10px 14px; text-align: left; }}
th {{ background: #f4f6f8; }} img {{ width: 100%; height: auto; }}
.note {{ color: #555; line-height: 1.6; }}
</style>
<h1>ETF 申赎套利回测报告</h1>
<table><tbody>{rows}</tbody></table>
<img src="{chart_name}" alt="累计 PnL 与回撤图">
<p class="note">基于日频账户总权益和已平仓套利交易计算。最大回撤以正数表示回撤幅度；盈亏比为平均盈利除以平均亏损绝对值；未平仓交易不计入胜率和盈亏比。</p>
{details}
</html>
"""
    path.write_text(document, encoding="utf-8")
