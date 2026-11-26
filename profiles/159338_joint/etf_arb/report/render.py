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


def suspension_pricing_note(etf_code):
    note = "本回测用统一交易日历确定现金替代 T+2 定价期限。停牌证券在期限内未成交时按既有规则估值，估值不产生股票成交或成交税费。"
    if str(etf_code) == "159338":
        note += "国泰 159338 的允许现金替代条款按被替代证券正常交易的两个交易日计算，长期停牌另有 20 个交易日的特别处理。"
    return note


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
    limit_value: dict | None = None,
    limit_exposure: dict | None = None,
    signal_risk: dict | None = None,
    limit_impact: dict | None = None,
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
               limit_value=limit_value, limit_exposure=limit_exposure,
               trade_rows=trade_rows, financial_summary=financial_summary,
               attribution_rows=attribution_rows, signal_risk=signal_risk, limit_impact=limit_impact)
    if attribution_rows is not None:
        from etf_arb.report.attribution import save_attribution_csv
        dynamic = bool(financial_summary and financial_summary.get("pricing_mode") == "dynamic_odd_lot_cash")
        save_attribution_csv(output / "trade_attribution.csv", attribution_rows, dynamic=dynamic)
    if limit_impact is not None:
        from etf_arb.report.limit_impact import save_limit_impact
        save_limit_impact(output, limit_impact)
    if signal_risk is not None:
        from etf_arb.report.signal_risk import save_signal_risk
        save_signal_risk(output, signal_risk)
    if limit_value is not None:
        from etf_arb.report.limit_value import save_limit_value
        save_limit_value(output, limit_value)
    if limit_exposure is not None:
        from etf_arb.report.limit_exposure import save_limit_exposure
        save_limit_exposure(output, limit_exposure)
    return summary


def _save_html(summary: BacktestSummary, path: Path, chart_name: str, *, trade_rows=None, financial_summary=None, limit_value=None, limit_exposure=None,
               attribution_rows=None, signal_risk=None, limit_impact=None) -> None:
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
    if signal_risk is not None:
        details += '<p><a href="signal_risk.html">查看信号时风险敞口：逐笔汇总与个股占比</a></p>'
    if financial_summary is not None:
        final = financial_summary['final_net_pnl']
        creation_threshold = financial_summary.get('creation_threshold', financial_summary.get('premium_threshold'))
        redemption_threshold = financial_summary.get('redemption_threshold', financial_summary.get('premium_threshold'))
        final_text = "待全部结清后确定" if final is None else f"{Decimal(final):,.2f} 元"
        direction = financial_summary.get('trade_direction', 'both')
        dynamic = financial_summary.get('pricing_mode') == 'dynamic_odd_lot_cash'
        limit_loss = financial_summary.get('limit_loss_threshold', False)
        comparison = '达到' if dynamic and not limit_loss else '严格大于'
        if limit_loss:
            sides = []
            if direction != 'redemption':
                sides.append(f"申购 {Decimal(creation_threshold):.4%}")
            if direction != 'creation':
                sides.append(f"赎回 {Decimal(redemption_threshold):.4%}")
            threshold_text = "本轮基础阈值为" + "、".join(sides) + "。"
        elif direction == 'redemption':
            threshold_text = f"本轮仅做赎回：折价幅度{comparison} {Decimal(redemption_threshold):.4%}。"
        elif direction == 'creation':
            threshold_text = f"本轮仅做申购：溢价{comparison} {Decimal(creation_threshold):.4%}。"
        else:
            threshold_text = (f"本轮门槛：申购溢价{comparison} {Decimal(creation_threshold):.4%}；"
                              f"赎回折价幅度{comparison} {Decimal(redemption_threshold):.4%}。")
        details += (f"<h2>发起期间的最终盈亏</h2><p>{escape(final_text)}；"
                    f"已结清 {financial_summary['settled_count']} 笔，待完成 {financial_summary['pending_count']} 笔。</p>"
                    f"<p>{threshold_text}"
                    "close 简化成交；定价完成与现金到账分别记录，待交收不计作最终结清。</p>")
        if dynamic:
            details += ("<p><strong>碎股动态折溢价</strong>。IOPV 中的现金部分按信号分钟更新，"
                        "有效敞口股数乘以相对当日调整前收的价格变化，汇总后加到盘前预估现金。"
                        "申赎预付与实际交收仍依据 PCF。"
                        f"因动态估值资料不足跳过 {financial_summary['dynamic_unpriced_minutes']} 个分钟，"
                        f"涉及 {len(financial_summary['dynamic_unpriced_dates'])} 个交易日。"
                        "运行目录的 signals.csv 记录各分钟原因。</p>")
        if limit_loss:
            title = "碎股动态估值与涨跌停分档阈值" if dynamic else "涨跌停分档阈值"
            basis = "碎股修正后的整篮估值" if dynamic else "信号时整篮估值"
            details += (f"<p><strong>{title}</strong>。"
                        f"将申购遇涨停、赎回遇跌停的逐股预计亏损加总，除以本笔{basis}后加到该方向的基础阈值上。"
                        "只有信号的折溢价幅度严格大于调整后阈值才继续检查开仓条件。"
                        "回放全段使用固定分档系数，校准期结果属于样本内对照。"
                        f"提高门槛后拦下 {financial_summary['limit_loss_blocks']} 个候选分钟。"
                        f"有 {financial_summary['limit_loss_incomplete_candidates']} 个候选分钟存在待核的报价或限价，"
                        "这些分钟的估计仅包含已识别的触限股票。")
            if (path.parent.parent / "signals.csv").is_file():
                details += ' <a href="../signals.csv">查看逐分钟折溢价、阈值与逐股预计亏损</a>。'
            details += "</p>"
        if financial_summary.get('filter_price_limits'):
            details += (
                "<p><strong>涨跌停过滤版</strong> · 检查当日 PCF 正数量成分股在信号和提交分钟的收盘价。"
                f"触及限价拦截 {financial_summary['price_limit_signal_blocks']} 个信号，"
                f"数据不足拦截 {financial_summary['price_limit_unknown_signal_blocks']} 个信号，"
                f"提交时再拦截 {financial_summary['price_limit_application_blocks']} 笔。"
                f"实际提交申购 {financial_summary['submitted_creation_count']} 笔，"
                f"赎回 {financial_summary['submitted_redemption_count']} 笔。</p>"
                "<p>提交被拦截时，已买入仓位从下一交易日开始尝试卖出，处置盈亏计入账户。"
                "逐笔表按原信号方向归属该盈亏，完整账本保留成交明细。</p>"
            )
        if financial_summary.get("check_creation_feasibility"):
            details += ("<p>申购可完成性检查：涨停无法买入时，仅在允许足额现金替代且合计未超过当日清单上限时保留信号。"
                        "比例按替代证券参考市值计算，不含保证金加成；参考价或限价无法核实时记录原因并拦截。"
                        f"候选分钟拦截 {financial_summary.get('creation_feasibility_block_count', 0)} 次，"
                        f"其中替代超限 {financial_summary.get('creation_cash_ratio_block_count', 0)} 次。</p>")
        if financial_summary.get("optional_creation_deposit_note"):
            details += f"<p>{escape(financial_summary['optional_creation_deposit_note'])}</p>"
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
            details += (f"<p>沪深代买卖时点分离：{proxy_market}跨市场申购代买、赎回代卖，从实际提交申赎起至各股票定价期限 15:00（含），"
                        "在首个可成交分钟使用精确 close 足额定价；期间逐分钟检查，含收盘最后一分钟。"
                        "整个窗口仍未成交的数量按本轮期限规则估值，不伪造成交或收取成交税费。"
                        + (f"赎回买入 ETF 使用信号 t 价格，正常可交易{domestic_market}实物股票在 t+1 申请分钟卖出。"
                           if financial_summary.get('entry_at_signal_time') else
                           f"赎回买入 ETF 及卖出正常可交易{domestic_market}实物股票使用信号后 t+1 分钟价格。") +
                        f"{domestic_market}可选申购现金替代补券按配置的后续时点尝试，默认在各股定价日 14:30。"
                        f"{domestic_market}无法卖出的实物股票继续持有并等待后续可交易分钟。"
                        "成交、现金替代最终金额确认和实际到账分别记录。</p>"
                        f"<p>赎回全部股票已在 t+1 锁价：{financial_summary.get('redemption_price_locked_count', 0)} 笔；"
                        f"含延迟卖出部分：{financial_summary.get('delayed_redemption_count', 0)} 笔。</p>")
        if financial_summary.get('settle_estimated_cash_at_application'):
            details += ("<p>申赎申请时按盘前 PCF 预估现金交收。下一交易日清单披露实际现金后，"
                        "申购按盘前预估减实际、赎回按实际减盘前预估进行差额交收，到账按本轮配置记录。</p>")
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
    if limit_exposure is not None:
        from etf_arb.report.limit_exposure import limit_exposure_html
        details += limit_exposure_html(limit_exposure)
    if limit_impact is not None:
        from etf_arb.report.limit_impact import limit_impact_html
        details += ('<details><summary>发起时触限的个股价格影响（不含税费，含未延迟成交）</summary>'
                    + limit_impact_html(limit_impact) + '</details>')
    if signal_risk is not None:
        from etf_arb.report.signal_risk import signal_risk_html
        details += ('<details><summary>信号时风险敞口与分类覆盖</summary>'
                    + signal_risk_html(signal_risk) + '</details>')
    if trade_rows is not None:
        if financial_summary and financial_summary.get("etf_code"):
            note = financial_summary.get("substitution_pricing_note") or suspension_pricing_note(financial_summary["etf_code"])
            details += f'<p class="note">{note}</p>'
        quality = ('<a href="../quality/summary.json">数据缺失检查</a>'
                   if (path.parent.parent / 'quality/summary.json').is_file()
                   else '本轮未执行缺失检查')
        details += ('<p><a href="ledger.html">逐笔全部现金、证券、成交及结算账目（可打印）</a> | '
                    + quality + ' | '
                    '<a href="../assumptions.json">本轮配置与假设</a></p>')
    appendix_link = ""
    if limit_value is not None:
        from etf_arb.report.limit_value_html import LIMIT_VALUE_LINK, limit_value_html
        details += limit_value_html(limit_value)
        appendix_link = LIMIT_VALUE_LINK
    report_title = "ETF 申赎套利回测报告"
    if financial_summary and financial_summary.get("strategy_mode") == "dynamic_odd_lot_cash_with_limit_loss":
        report_title = f"{escape(str(financial_summary['etf_code']))} 碎股动态与涨跌停联合回测报告"
    document = f"""<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{report_title}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 1100px; margin: 32px auto; padding: 0 20px; color: #20252b; }}
body.wide-report {{ max-width: none; }}
table {{ border-collapse: collapse; min-width: 420px; margin: 20px 0; }}
th, td {{ border-bottom: 1px solid #ddd; padding: 10px 14px; text-align: left; }}
th {{ background: #f4f6f8; }} img {{ width: 100%; max-width:1100px; height: auto; }}
.note {{ color: #555; line-height: 1.6; }}
</style>
<body class="{'wide-report' if attribution_rows is not None or limit_value is not None else ''}">
<h1>{report_title}</h1>
{appendix_link}
<table><tbody>{rows}</tbody></table>
<img src="{chart_name}" alt="累计 PnL 与回撤图">
<p class="note">基于日频账户总权益和已平仓套利交易计算。最大回撤以正数表示回撤幅度；盈亏比为平均盈利除以平均亏损绝对值；未平仓交易不计入胜率和盈亏比。</p>
{details}
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")
