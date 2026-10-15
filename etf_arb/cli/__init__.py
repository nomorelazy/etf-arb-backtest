"""命令行入口。"""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from etf_arb import __version__
from etf_arb.data import ParquetMinuteCloseRepository
from etf_arb.report.io import load_equity_csv, load_trades_csv
from etf_arb.report.render import save_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="etf-arb",
        description="ETF creation/redemption arbitrage backtest framework.",
    )
    parser.add_argument("--version", action="version", version=f"etf-arb {__version__}")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="show the current implementation stage")

    minute_scan_parser = subparsers.add_parser(
        "scan-minute-close",
        help="scan monthly iFinD minute-close Parquet files and update the SQLite catalog",
    )
    minute_scan_parser.add_argument("--root", required=True, help="directory containing YYYY-MM Parquet folders")
    minute_scan_parser.add_argument("--catalog", help="optional SQLite catalog path")
    minute_scan_parser.add_argument("--force", action="store_true", help="re-read unchanged files")

    report_parser = subparsers.add_parser("report", help="build an HTML/PNG report from backtest CSV files")
    report_parser.add_argument("--equity", required=True, help="CSV with date,equity columns")
    report_parser.add_argument("--trades", help="optional CSV with trade_id,net_pnl columns")
    report_parser.add_argument("--starting-cash", required=True, type=Decimal)
    report_parser.add_argument("--output", default="reports/latest", help="report output directory")
    report_parser.add_argument("--risk-free-rate", type=float, default=0.0, help="annual risk-free rate, e.g. 0.02")
    report_parser.add_argument("--periods-per-year", type=int, default=252)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "status":
        print("Core models, PCF parsing, iFinD minute-close storage and basket pricing are available.")
        print("Historical market rules, fill simulation and event backtest are pending.")
    elif args.command == "scan-minute-close":
        repository = ParquetMinuteCloseRepository(
            args.root,
            catalog_path=args.catalog,
            auto_scan=False,
        )
        report = repository.scan(force=args.force)
        print(f"Parquet files found: {report.files_found}")
        print(f"Files indexed:       {report.files_indexed}")
        print(f"Files unchanged:     {report.files_unchanged}")
        print(f"Files removed:       {report.files_removed}")
        print(f"Rows cataloged:      {report.rows_indexed}")
        print(f"Securities cataloged:{report.securities_indexed:>7}")
        print(f"SQLite catalog:      {report.catalog_path}")
    elif args.command == "report":
        equity = load_equity_csv(args.equity)
        trades = load_trades_csv(args.trades) if args.trades else []
        summary = save_report(
            equity,
            trades,
            args.output,
            starting_cash=args.starting_cash,
            annual_risk_free_rate=args.risk_free_rate,
            periods_per_year=args.periods_per_year,
        )
        output = Path(args.output)
        print(f"HTML report: {output / 'report.html'}")
        print(f"PNG chart:   {output / 'pnl_report.png'}")
        print(f"Metrics CSV: {output / 'metrics.csv'}")
    return 0
