"""命令行入口。"""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

from etf_arb import __version__
from etf_arb.data import ParquetMinuteCloseRepository, check_missing


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

    missing_parser = subparsers.add_parser(
        "check-missing", help="check PCF and exact minute-price presence without changing source data"
    )
    missing_parser.add_argument("--pcf-root", required=True)
    missing_parser.add_argument("--minute-root", required=True)
    missing_parser.add_argument("--etf", required=True, nargs="+", help="ETF codes, e.g. 159338 512760")
    missing_parser.add_argument("--start", required=True, type=date.fromisoformat)
    missing_parser.add_argument("--end", required=True, type=date.fromisoformat)
    missing_parser.add_argument(
        "--trading-days", help="UTF-8 text: one YYYY-MM-DD per trading day; # comments allowed"
    )
    missing_parser.add_argument("--output", default="reports/missing")

    backtest_parser = subparsers.add_parser(
        "backtest", help="run decision replay or the configured full-flow research simulation"
    )
    backtest_parser.add_argument("--config", required=True, help="Runner JSON configuration")
    backtest_parser.add_argument("--premium-threshold", type=Decimal,
                                 help="override signal threshold as a ratio (e.g. 0.005 means 0.5%%)")

    report_parser = subparsers.add_parser("report", help="build an HTML/PNG report from backtest CSV files")
    report_parser.add_argument("--equity", required=True, help="CSV with date,equity columns")
    report_parser.add_argument("--trades", help="optional CSV with trade_id,net_pnl columns")
    report_parser.add_argument("--starting-cash", required=True, type=Decimal)
    report_parser.add_argument("--output", default="reports/latest", help="report output directory")
    report_parser.add_argument("--risk-free-rate", type=float, default=0.0, help="annual risk-free rate, e.g. 0.02")
    report_parser.add_argument("--periods-per-year", type=int, default=252)
    attribution_parser = subparsers.add_parser('report-attribution',
        help='rebuild a detailed report from an existing completed run without executing trades')
    attribution_parser.add_argument('--run-dir', required=True)
    attribution_parser.add_argument('--output', required=True, help='fresh report output folder')
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "status":
        print("Core models, PCF parsing, iFinD minute-close storage and basket pricing are available.")
        print("BacktestRunner supports configuration, quality preflight, clock callbacks and decision replay.")
        from etf_arb.data.snapshot import FULL_FLOW_ETFS
        print(f"Full-flow research simulation (one ETF per run): {', '.join(FULL_FLOW_ETFS)}.")
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
    elif args.command == "check-missing":
        days = None
        if args.trading_days:
            lines = Path(args.trading_days).read_text(encoding="utf-8-sig").splitlines()
            days = [
                date.fromisoformat(line.strip())
                for line in lines if line.strip() and not line.lstrip().startswith("#")
            ]
        print("Checking missing data (source minute grid: 242 timestamps/day)...", flush=True)
        report = check_missing(
            pcf_root=args.pcf_root, minute_root=args.minute_root, etf_codes=args.etf,
            start_date=args.start, end_date=args.end, trading_days=days,
        )
        summary_path, detail_path = report.save(args.output)
        summary = report.summary()
        print(f"Calendar source:     {report.calendar_source}")
        print(f"Dates checked:       {len(report.checked_dates)}")
        print(f"Missing issues:      {summary['missing_issue_count']}")
        print(f"Missing minutes:     {summary['missing_minute_count']}")
        print(f"Unchecked issues:    {summary['unchecked_issue_count']}")
        print(f"Summary: {summary_path}")
        print(f"Details: {detail_path}")
        if report.calendar_source == "observed":
            print("Reference dates are observed, not an exchange calendar; days absent from both sources are unknown.")
        return 1 if report.issues else 0
    elif args.command == "backtest":
        from etf_arb.runner import BacktestRunner, RunnerError
        from etf_arb.runner_config import RunnerConfig
        from etf_arb.full_flow import make_full_runner

        try:
            config = RunnerConfig.from_json(args.config)
            if args.premium_threshold is not None:
                if config.simulation is None:
                    raise ValueError("--premium-threshold requires a simulation configuration")
                config = replace(config, simulation=replace(config.simulation, premium_threshold=args.premium_threshold))
            runner = make_full_runner(config) if config.simulation is not None else BacktestRunner(config)
            result = runner.run()
        except (RunnerError, ValueError, TypeError, OSError) as error:
            print(f"Backtest failed: {error}")
            return 2
        print(f"Mode: {result.mode}")
        print(f"Trading days: {len(result.processed_dates)}")
        print(f"Decisions: {result.engine_result.decision_count}")
        print(f"Dispatched: {result.engine_result.dispatched_count}")
        print(f"Outputs: {result.output_dir}")
        if result.financial_summary is None:
            print("This is decision replay; no trade profit or account equity has been calculated.")
        else:
            print(f"Research simulation final PnL: {result.financial_summary['final_net_pnl']}")
            print(f"Pending trades: {result.financial_summary['pending_count']}")
            print(f"HTML report: {result.output_dir / 'report' / 'report.html'}")
    elif args.command == 'report-attribution':
        from etf_arb.report.rebuild import rebuild_run_report
        try:
            report = rebuild_run_report(args.run_dir, args.output)
        except (ValueError, TypeError, OSError) as error:
            print(f'Report rebuild failed: {error}')
            return 2
        print(f'HTML report: {report}')
        print('Report only; no backtest was started.')
    elif args.command == "report":
        from etf_arb.report.io import load_equity_csv, load_trades_csv
        from etf_arb.report.render import save_report

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
