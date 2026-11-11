"""PNG 可视化与 CSV 指标报告。"""

from __future__ import annotations

import csv
from dataclasses import fields
from decimal import Decimal
from pathlib import Path
from html import escape
from typing import Sequence

from etf_arb.report.metrics import calculate_summary
from etf_arb.report.models import DEFAULT_ANNUAL_RISK_FREE_RATE, BacktestSummary, ClosedTrade, EquityPoint


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
    "annual_risk_free_rate",
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
    annual_risk_free_rate: float = DEFAULT_ANNUAL_RISK_FREE_RATE,
    periods_per_year: int = 252,
    peak_cash_usage: Decimal = Decimal("0"),
    filled_orders: int = 0,
    rejected_orders: int = 0,
    failed_applications: int = 0,
    title: str = "ETF Arbitrage Backtest",
    trade_rows: Sequence[dict] | None = None,
    financial_summary: dict | None = None,
    attribution_rows: Sequence[dict] | None = None,
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
        f"Sharpe (Rf {summary.annual_risk_free_rate:.2%}): {_format_metric(summary.sharpe_ratio)}",
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
               trade_rows=trade_rows, financial_summary=financial_summary,
               attribution_rows=attribution_rows)
    if attribution_rows is not None:
        from etf_arb.report.attribution import save_attribution_csv
        save_attribution_csv(output / "trade_attribution.csv", attribution_rows)
    return summary


def _save_html(summary: BacktestSummary, path: Path, chart_name: str, *, trade_rows=None, financial_summary=None,
               attribution_rows=None) -> None:
    labels = {
        "total_return": "区间收益率",
        "annualized_return": "年化收益率",
        "max_drawdown": "最大回撤",
        "sharpe_ratio": "夏普比率",
        "annual_risk_free_rate": "年化无风险利率",
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
        "annual_risk_free_rate": _format_percent(summary.annual_risk_free_rate),
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
        creation_threshold = financial_summary.get('creation_threshold', financial_summary.get('premium_threshold'))
        redemption_threshold = financial_summary.get('redemption_threshold', financial_summary.get('premium_threshold'))
        final_text = "待全部结清后确定" if final is None else f"{Decimal(final):,.2f} 元"
        details += (f"<h2>发起期间的最终盈亏</h2><p>{escape(final_text)}；"
                    f"已结清 {financial_summary['settled_count']} 笔，待完成 {financial_summary['pending_count']} 笔。</p>"
                    f"<p>本轮门槛：申购溢价严格大于 {Decimal(creation_threshold):.4%}；"
                    f"赎回折价幅度严格大于 {Decimal(redemption_threshold):.4%}。"
                    "close 简化成交；定价完成与现金到账分别记录，待交收不计作最终结清。</p>")
        if financial_summary.get('frequency_anchor') == 'application_submitted_at':
            details += (f"<p>交易节奏：以上一笔实际提交申赎时间起算，满 {financial_summary['min_trade_interval_minutes']} 分钟后，"
                        "有合格信号且可用现金足够即可发起下一笔，允许多笔未结清套利并行。"
                        f"本轮未结清套利数量峰值：{financial_summary['peak_open_cycles']} 笔。</p>")
        if financial_summary.get('market_split_execution'):
            if financial_summary.get('entry_at_signal_time'):
                details += ("<p>本轮买入时点：赎回买入 ETF、申购买入实物股票均使用信号 t 分钟的精确 close；"
                            "t+1 分钟提交申赎，正常可交易的赎回股票在该分钟卖出；"
                            + ("申购 ETF 在 t+1 分钟提交申购并取得份额后，在同一分钟按精确 close 卖出。"
                               if financial_summary.get('creation_etf_sale_at_application') else
                               "申购 ETF 在申请后的首个允许成交分钟卖出，通常为信号后的第 2 分钟。")
                            + "缺精确卖价或不允许卖出时保留持仓，按后续实际成交记录，不伪造 t+1 成交。</p>")
            listed_in_sh = str(financial_summary.get('etf_code', '159338')).startswith('5')
            domestic_market, proxy_market = ('沪市', '深市') if listed_in_sh else ('深市', '沪市')
            details += (f"<p>沪深代买卖时点分离：{proxy_market}跨市场申购代买、赎回代卖，从实际提交申赎起至 T+2 日 15:00（含），"
                        "在首个可成交分钟使用精确 close 足额定价；期间逐分钟检查，含收盘最后一分钟。"
                        "整个窗口仍未成交的数量按 T+2 日精确收盘价估值，不伪造成交或收取成交税费。"
                        + (f"赎回买入 ETF 使用信号 t 价格，正常可交易{domestic_market}实物股票在 t+1 申请分钟卖出。"
                           if financial_summary.get('entry_at_signal_time') else
                           f"赎回买入 ETF 及卖出正常可交易{domestic_market}实物股票使用信号后 t+1 分钟价格。") +
                        f"{domestic_market}可选申购现金替代补券按配置的后续时点尝试，默认 T+2 日 14:30。"
                        f"{domestic_market}无法卖出的实物股票继续持有并等待后续可交易分钟。"
                        "成交、现金替代最终金额确认和实际到账分别记录。</p>"
                        f"<p>赎回全部股票已在 t+1 锁价：{financial_summary.get('redemption_price_locked_count', 0)} 笔；"
                        f"含延迟卖出部分：{financial_summary.get('delayed_redemption_count', 0)} 笔。</p>")
        if financial_summary.get('settle_estimated_cash_at_application'):
            details += ("<p>申赎申请时按当日预估现金 E 交收。下一交易日清单披露实际现金 A 后，"
                        "申购按 E−A、赎回按 A−E 进行差额交收，到账按本轮配置记录。</p>")
        if 'fee_breakdown' in financial_summary:
            details += (f"<h2>交易税费</h2><p>成交税费合计 {Decimal(financial_summary['fee']):,.2f} 元；"
                        f"其中投资者账户扣款 {Decimal(financial_summary['investor_fees']):,.2f} 元，"
                        f"代买卖费用 {Decimal(financial_summary['manager_fees']):,.2f} 元计入现金替代退补款。</p>")
            from etf_arb.report.ledger import _table
            names = {'commission': '佣金', 'stamp_duty': '印花税', 'transfer_fee': '过户费', 'handling_fee': '经手费'}
            details += _table([{'费用项目': label, '金额（元）': financial_summary['fee_breakdown'][key]}
                               for key, label in names.items()])
    if attribution_rows is not None:
        from etf_arb.report.attribution import attribution_html
        details += attribution_html(attribution_rows)
    if trade_rows is not None:
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
body.wide-report {{ max-width: none; }}
table {{ border-collapse: collapse; min-width: 420px; margin: 20px 0; }}
th, td {{ border-bottom: 1px solid #ddd; padding: 10px 14px; text-align: left; }}
th {{ background: #f4f6f8; }} img {{ width: 100%; max-width:1100px; height: auto; }}
.note {{ color: #555; line-height: 1.6; }}
</style>
<body class="{'wide-report' if attribution_rows is not None else ''}">
<h1>ETF 申赎套利回测报告</h1>
<table><tbody>{rows}</tbody></table>
<img src="{chart_name}" alt="累计 PnL 与回撤图">
<p class="note">基于日频账户总权益和已平仓套利交易计算。最大回撤以正数表示回撤幅度；盈亏比为平均盈利除以平均亏损绝对值；未平仓交易不计入胜率和盈亏比。</p>
{details}
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")
