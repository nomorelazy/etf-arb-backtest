"""回测指标、报告数据结构与图表输出。"""

from etf_arb.report.metrics import calculate_summary
from etf_arb.report.io import load_equity_csv, load_trades_csv
from etf_arb.report.models import BacktestSummary, ClosedTrade, EquityPoint
from etf_arb.report.render import save_report

__all__ = [
    "BacktestSummary",
    "ClosedTrade",
    "EquityPoint",
    "calculate_summary",
    "load_equity_csv",
    "load_trades_csv",
    "save_report",
]
