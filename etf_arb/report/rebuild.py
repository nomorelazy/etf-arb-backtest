"""Regenerate the standard detailed report from saved outputs without trading."""

from collections import defaultdict
import csv
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path

import pyarrow.dataset as ds

from etf_arb.data.pcf import FilePCFRepository
from etf_arb.execution.plan import build_plan
from etf_arb.domain import Side
from etf_arb.report.attribution import (SIGNAL_BASIS_FIELDS, build_trade_attribution,
    decimal, signal_reference, timestamp)
from etf_arb.report.io import load_equity_csv
from etf_arb.report.ledger import save_ledger_report
from etf_arb.report.models import ClosedTrade
from etf_arb.report.render import save_report


def read_rows(path):
    if not path.is_file():
        return []
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def restore_signal_references(config, trades, decisions, substitutions):
    """Legacy reports lack signal snapshots. Query only t / t-1 saved market bars."""
    decision_by_time = {(timestamp(r['ts']), r['side']): r for r in decisions}
    for trade in trades:
        decision = decision_by_time.get((timestamp(trade['opened_at']), trade['side']))
        if decision is not None:
            trade.setdefault('signal_gross_edge', decision['gross_edge'])
    missing = [t for t in trades if any(t.get(name) in (None, '') for name in SIGNAL_BASIS_FIELDS)]
    if not missing:
        return
    pcfs = FilePCFRepository(config['pcf_root'])
    substitutes = defaultdict(dict)
    for row in substitutions:
        substitutes[row['trade_id']][row['symbol']] = decimal(row['deposit'])
    plans, by_month = {}, defaultdict(lambda: dict(times=set(), symbols=set()))
    for trade in missing:
        at, units = timestamp(trade['opened_at']), int(trade['quantity_units'])
        pcf = pcfs.get(config.get('etf_codes', ['159338'])[0], at.date())
        selected = {symbol: amount / units for symbol, amount in substitutes[trade['trade_id']].items()}
        plan = build_plan(pcf, Side(trade['side']), units,
                          creation_cash_substitutions=selected if selected else None)
        plans[trade['trade_id']] = pcf, plan
        request = by_month[at.strftime('%Y-%m')]
        request['times'].update((at, at-timedelta(minutes=1)))
        request['symbols'].update(plan.physical | plan.manager)
    quotes = {}
    for month, request in sorted(by_month.items()):
        files = sorted((Path(config['minute_root']) / month).glob('*.parquet'))
        if not files or not request['symbols']:
            continue
        table = ds.dataset([str(p) for p in files], format='parquet').to_table(
            columns=['time', 'thscode', 'close'],
            filter=ds.field('time').isin(sorted(request['times']))
                   & ds.field('thscode').isin(sorted(request['symbols'])))
        for at, symbol, raw in zip(*(table[n].to_pylist() for n in ('time', 'thscode', 'close'))):
            if raw is None:
                continue
            price = decimal(raw)
            if price <= 0:
                raise ValueError(f'Invalid historical signal quote: {symbol} {at}')
            key = symbol, at
            if key in quotes and quotes[key] != price:
                raise ValueError(f'Conflicting historical signal quote: {symbol} {at}')
            quotes[key] = price
    for trade in missing:
        at = timestamp(trade['opened_at'])
        def price_at(symbol):
            return quotes.get((symbol, at), quotes.get((symbol, at-timedelta(minutes=1))))
        pcf, plan = plans[trade['trade_id']]
        trade.update(signal_reference(plan, pcf, int(trade['quantity_units']), price_at))


def rebuild_run_report(run_dir, output_dir):
    run_dir, output_dir = Path(run_dir).resolve(), Path(output_dir).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ValueError('Report output directory is not empty; choose a fresh folder')
    config = json.loads((run_dir / 'config.json').read_text(encoding='utf-8'))
    summary = json.loads((run_dir / 'financial_summary.json').read_text(encoding='utf-8'))
    trades = read_rows(run_dir / 'trades.csv')
    fills = read_rows(run_dir / 'fills.csv')
    settlements = read_rows(run_dir / 'cash_settlements.csv')
    fees = read_rows(run_dir / 'fees.csv')
    pricing = read_rows(run_dir / 'substitution_pricing.csv')
    substitutions = read_rows(run_dir / 'creation_substitutions.csv')
    restore_signal_references(config, trades, read_rows(run_dir / 'decisions.csv'), substitutions)
    attribution = build_trade_attribution(trades, read_rows(run_dir / 'signals.csv'), fills,
                                         settlements, fees, pricing)
    closed = [ClosedTrade(t['trade_id'], decimal(t['net_pnl'])) for t in trades
              if t['state'] == 'CLOSED' and t.get('net_pnl') not in (None, '')]
    actual_net = sum((t.pnl for t in closed), Decimal(0))
    if summary['final_net_pnl'] is not None and actual_net != decimal(summary['final_net_pnl']):
        raise ValueError('Saved final PnL does not reconcile to the trade ledger')
    equity = load_equity_csv(run_dir / 'report/equity.csv')
    metrics = {row['metric']: row['value'] for row in read_rows(run_dir / 'report/metrics.csv')}
    save_report(equity, closed, output_dir, starting_cash=decimal(config['starting_cash']),
                total_costs=decimal(summary['fee']), trade_rows=read_rows(run_dir / 'portfolio_ledger.csv'),
                financial_summary=summary, attribution_rows=attribution,
                peak_cash_usage=decimal(metrics.get('peak_cash_usage', '0')),
                filled_orders=int(metrics.get('filled_orders', '0')),
                rejected_orders=int(metrics.get('rejected_orders', '0')),
                failed_applications=int(metrics.get('failed_applications', '0')),
                title=f"{config.get('etf_codes', ['159338'])[0]} Research simulation "
                      "(configured fees, slippage=0; proxy cash settlement)")
    save_ledger_report(output_dir / 'ledger.html', trades, read_rows(run_dir / 'cashflows.csv'),
        fills, settlements, pricing, read_rows(run_dir / 'substitution_attempts.csv'), substitutions,
        fees, attribution_rows=attribution)
    (output_dir / 'report_source.json').write_text(json.dumps(dict(
        source_run=str(run_dir), source_config=config, financial_summary=summary,
        report_only=True, trade_count=len(attribution),
        definition='cash substitution PnL is relative to signal value including manager fees; refund is not profit'),
        ensure_ascii=False, indent=2), encoding='utf-8')
    return output_dir / 'report.html'
