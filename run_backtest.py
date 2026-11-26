"""Run the 159338 odd-lot and price-limit loss strategy."""

import argparse
from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
JOINT_PROFILE = ROOT / 'profiles' / '159338_joint'
sys.path.insert(0, str(JOINT_PROFILE))

import etf_arb
from etf_arb.report.run_paths import create_report_run
from etf_arb.runner_config import RunnerConfig, load_rules


def main():
    root = ROOT
    if Path(etf_arb.__file__).resolve().parent.parent != JOINT_PROFILE:
        raise RuntimeError(f'The joint backtest must load its engine from {JOINT_PROFILE}.')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path,
                        default=root / 'configs/159338_joint.json')
    parser.add_argument('--etf', choices=('159338',), help='ETF with a calibrated price-limit loss table')
    parser.add_argument('--premium-threshold', type=Decimal)
    parser.add_argument('--creation-threshold', type=Decimal)
    parser.add_argument('--redemption-threshold', type=Decimal)
    parser.add_argument('--direction', choices=('both', 'creation', 'redemption'))
    parser.add_argument('--start-date', type=date.fromisoformat, help='First signal date, YYYY-MM-DD')
    parser.add_argument('--end-date', type=date.fromisoformat, help='Last signal date, YYYY-MM-DD')
    parser.add_argument('--settlement-end-date', type=date.fromisoformat, help='Final settlement date, YYYY-MM-DD')
    parser.add_argument('--pcf-root', type=Path, help='Local PCF data directory')
    parser.add_argument('--minute-root', type=Path, help='Local minute-close data directory')
    parser.add_argument('--odd-lot-holdings', type=Path, help='Daily ETF holdings workbook')
    parser.add_argument('--odd-lot-reference-prices', type=Path, help='Daily adjusted preClose CSV')
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--daily-price-limits', type=Path, help='Optional official daily limit prices CSV')
    parser.add_argument('--adjusted-reference-prices', type=Path, help='Dated ex-rights reference prices with source URLs')
    parser.add_argument('--stock-suspensions', type=Path, help='Sourced historical stock suspension intervals')
    parser.add_argument('--creation-reference-prices', type=Path,
                        help='Optional dated adjusted reference prices: date,symbol,reference_price')
    args = parser.parse_args()
    config = RunnerConfig.from_json(args.config)
    first = args.start_date or config.start_date
    last = args.end_date or config.end_date
    until = args.settlement_end_date or config.settlement_end_date
    if first < config.trading_days[0] or until > config.trading_days[-1]:
        parser.error('The requested dates exceed the trading calendar supplied by this config.')
    holdings = args.odd_lot_holdings or config.odd_lot_holdings_path
    references = args.odd_lot_reference_prices or config.odd_lot_reference_prices_path
    if holdings is None or references is None:
        parser.error('This dynamic launcher needs both the holdings workbook and the preClose CSV.')
    for path in (holdings, references):
        if not path.is_file():
            parser.error(f'Missing dynamic valuation input: {path}')
    config = replace(config, pcf_root=args.pcf_root or config.pcf_root,
                     minute_root=args.minute_root or config.minute_root,
                     start_date=first, end_date=last, settlement_end_date=until,
                     odd_lot_holdings_path=holdings, odd_lot_reference_prices_path=references,
                     daily_price_limits_path=args.daily_price_limits or config.daily_price_limits_path,
                     adjusted_reference_prices_path=args.adjusted_reference_prices or config.adjusted_reference_prices_path,
                     stock_suspensions_path=args.stock_suspensions or config.stock_suspensions_path,
                     creation_reference_prices_path=args.creation_reference_prices or config.creation_reference_prices_path)
    if config.simulation is None or not config.simulation.limit_loss_threshold:
        parser.error('This combined launcher requires simulation.limit_loss_threshold=true.')
    if config.filter_price_limits:
        parser.error('Use the directional loss adjustment with filter_price_limits=false.')
    simulation = config.simulation.with_threshold_overrides(
        premium_threshold=args.premium_threshold, creation_threshold=args.creation_threshold,
        redemption_threshold=args.redemption_threshold)
    if args.direction is not None:
        simulation = replace(simulation, trade_direction=args.direction)
    config = replace(config, simulation=simulation)
    creation = simulation.effective_creation_threshold
    redemption = simulation.effective_redemption_threshold
    if creation is None or redemption is None:
        parser.error('Set a shared premium threshold or both directional thresholds.')
    for source in (config.pcf_root, config.minute_root):
        if not source.is_dir():
            raise FileNotFoundError(source)
    load_rules(config.rules_path).get('159338', config.trading_days[0])
    print(f'Source: {root}', flush=True)
    print(f'Dynamic cash: {holdings} (零股数量); adjusted preClose: {references}', flush=True)
    print('ETF: 159338', flush=True)
    print(f'Creation feasibility: {simulation.check_creation_feasibility}', flush=True)
    print(f'Price-limit filter: {"on" if config.filter_price_limits else "off"}', flush=True)
    print(f'Trade direction: {config.simulation.trade_direction}', flush=True)
    print(f'Base thresholds: creation {creation:.4%}, redemption {redemption:.4%}', flush=True)
    print('Entry requires dynamic premium/discount magnitude strictly above the base '
          'plus estimated directional stock loss / dynamic basket value.', flush=True)
    print(f'Opening dates: {config.start_date} through {config.end_date}', flush=True)
    print(f'Settlement through: {config.settlement_end_date}', flush=True)
    print(f'Starting cash: {config.starting_cash:,.2f}', flush=True)
    print(f'Entry at signal t: {config.simulation.entry_at_signal_time}', flush=True)
    print(f'Creation ETF sale at t+1: '
          f'{config.simulation.entry_at_signal_time and config.simulation.market_split_execution}', flush=True)
    print(f'Research cash lags: substitution={config.simulation.cash_substitution_lag}, '
          f'cash difference={config.simulation.cash_difference_lag}', flush=True)
    if args.check_only:
        print('Configuration checked; no backtest started.', flush=True)
        return 0
    creation_tag, redemption_tag = (format((value * 100).normalize(), 'f').replace('.', 'p')
                                     for value in (creation, redemption))
    label = {'both': f'creation{creation_tag}_redemption{redemption_tag}',
             'creation': f'creation_only{creation_tag}', 'redemption': f'redemption_only{redemption_tag}'}[
                 simulation.trade_direction]
    run_id, run_directory, configs = create_report_run(
        config, config.etf_codes, root / 'reports', 'dynamic_limit_loss_' + label,
    )
    print(f'Run directory: {run_directory}', flush=True)
    snapshots = root / 'configs' / 'generated'
    snapshots.mkdir(parents=True, exist_ok=True)
    fund_config, = configs
    snapshot = snapshots / f'run_{run_id}.json'
    with snapshot.open('x', encoding='utf-8') as handle:
        json.dump(fund_config.to_dict(), handle, ensure_ascii=False, indent=2)
    print(f'159338 report: {fund_config.output_dir / "report/report.html"}', flush=True)
    return subprocess.call([sys.executable, '-X', 'utf8', str(JOINT_PROFILE / 'run_cli.py'),
                            'backtest', '--config', str(snapshot)], cwd=root)


if __name__ == '__main__':
    raise SystemExit(main())
