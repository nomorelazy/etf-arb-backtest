"""Read-only exposure intervals backed by directional limit-block records."""

from collections import defaultdict
import csv
from datetime import datetime, time, timedelta
from decimal import Decimal
from html import escape
import json
from pathlib import Path
from urllib.parse import quote

from etf_arb.data.adjusted_references import AdjustedReferences
from etf_arb.data.pcf import FilePCFRepository
from etf_arb.data.snapshot import component_symbol
from etf_arb.report.limit_impact import EventPrices
from etf_arb.report.price_limit_bounds import ReportPriceLimits
from etf_arb.report.stock_cost import cost_metrics, decimal, timestamp
from etf_arb.rules.suspensions import StockSuspensions, with_suspensions
from etf_arb.rules import InMemoryRuleRepository
from etf_arb.runner_config import load_rules

ZERO = Decimal(0)
FULL_CYCLE_BASIS = 'price_change_since_trade_open_excluding_fees_not_causal_estimate'
PLANNED_EXECUTION_BASIS = 'price_change_since_planned_stock_execution_excluding_fees_not_causal_estimate'
FIELDS = (
    'trade_id', 'order_id', 'opened_at', 'side', 'symbol', 'security_name', 'initial_limit_state', 'purpose',
    'exposure_type', 'first_blocked_at', 'last_limit_blocked_at', 'observed_limit_minutes',
    'exposure_end_at', 'observed_through', 'elapsed_minutes', 'cross_day', 'status',
    'planned_at', 'planned_timing_basis',
    'initial_quantity', 'filled_quantity', 'filled_after_block', 'valued_quantity', 'untraded_quantity',
    'unpriced_quantity', 'initial_price', 'initial_price_at', 'initial_price_source',
    'blocked_price', 'blocked_quantity', 'blocked_elapsed_minutes',
    'initial_exposure', 'first_fill_at', 'last_fill_at',
    'fill_price', 'valuation_at', 'valuation_price', 'mark_at', 'mark_price', 'remaining_exposure',
    'realized_price_pnl', 'valuation_price_pnl', 'unrealized_price_pnl',
    'period_price_pnl', 'etf_shares', 'basket_value', 'etf_cost_impact',
    'etf_cost_impact_per_share', 'etf_cost_impact_ratio', 'paid_at', 'limit_sources',
)


def _setting(config, name, default=None):
    return config.get(name, default) if isinstance(config, dict) else getattr(config, name, default)


def _sum(values):
    values = list(values)
    return None if any(v is None for v in values) else sum(values, ZERO)


def _quantity(value):
    value = decimal(value)
    if value is None or value < 0 or value != value.to_integral_value():
        raise ValueError('Exposure quantities must be known nonnegative whole shares')
    return int(value)


def price_only_limit_exposure(data):
    """Upgrade saved reports from price components, without changing source rows.

    Never recover price PnL from rounded net amounts or treat unknown marks as
    zero. Fees stay in the execution/account ledgers, outside this report.
    """
    rows = []
    full_cycle = data.get('pnl_basis') == FULL_CYCLE_BASIS
    planned = data.get('pnl_basis') == PLANNED_EXECUTION_BASIS
    for original in data['rows']:
        row = dict(original)
        if row.get('initial_limit_state') not in {'limit_up', 'limit_down'}:
            # V1/V2 caches contain only direction-matched recorded blocks:
            # CREATE upper-limit buys and REDEEM lower-limit sells. Do not
            # apply this inference to arbitrary trade/signal reports.
            row['initial_limit_state'] = (
                {'CREATE': 'limit_up', 'REDEEM': 'limit_down'}.get(row.get('side'))
                if data.get('selection_basis') == 'recorded_directional_limit_delay' else None)
        gross = _sum(decimal(row.get(k)) for k in (
            'realized_price_pnl', 'valuation_price_pnl', 'unrealized_price_pnl'))
        row['period_price_pnl'] = gross
        if full_cycle:
            row['full_cycle_price_pnl'] = gross
        if planned:
            row.pop('full_cycle_price_pnl', None)
        metrics = cost_metrics(gross, ZERO, row.get('etf_shares'), row.get('basket_value'))
        row.update({k: metrics[k] for k in (
            'etf_cost_impact', 'etf_cost_impact_per_share', 'etf_cost_impact_ratio')})
        row.pop('incurred_fees', None)
        row.pop('period_pnl', None)
        rows.append(row)
    return dict(data, rows=rows, schema_version=5 if planned else 4 if full_cycle else 3,
                initial_limit_state_basis='first_recorded_directional_block',
                pnl_basis=PLANNED_EXECUTION_BASIS if planned else FULL_CYCLE_BASIS if full_cycle else 'price_change_since_first_block_excluding_fees_not_causal_estimate',
                fee_policy='exclude_from_exposure_report_keep_execution_and_account_ledgers')


def planned_execution_limit_exposure(data, config, trades, orders, fills, pricing,
                                     *, substitutions=(), stock_rows=(), settlements=()):
    """Price selected orders from their intended stock-execution minute.

    Old caches only contain post-block fills and prices. Reconstruct the whole
    order from saved records; neither signal nor first-block prices may silently
    replace a missing exact planned-minute price. This never changes cash.
    """
    by_order = {r['order_id']: r for r in orders}
    if data.get('pnl_basis') == PLANNED_EXECUTION_BASIS:
        # An intermediate cache may contain a basket-level 14:30 timestamp.
        # Rebuild it only if the recorded order carries a different schedule.
        stale = any(row.get('planned_timing_basis') == 'configured_legacy_manager_attempt'
                    and 'attempt_scheduled_at' in by_order.get(row['order_id'], {})
                    and str(by_order[row['order_id']]['attempt_scheduled_at'] or '') != str(row.get('planned_at') or '')
                    for row in data['rows'])
        if not stale:
            return price_only_limit_exposure(data)
    by_trade = {r['trade_id']: r for r in trades}
    legs = {r['trade_id']: r for r in settlements if r['kind'] == 'cash_substitution'}
    settings = _setting(config, 'simulation', {})
    one_minute = _setting(settings, 'one_minute_execution')
    split = _setting(settings, 'market_split_execution', True if one_minute is None else one_minute)
    signal_entry = _setting(settings, 'entry_at_signal_time', one_minute is None)
    from etf_arb.rules import TradingTimeRule
    trading = TradingTimeRule(_setting(settings, 'blocked_trade_minutes', ()),
                             _setting(settings, 'trading_sessions', (('09:30','11:30'),('13:00','14:57'))))
    def next_investor_minute(at):
        candidates = [datetime.combine(at.date(), time.fromisoformat(str(clock)))
                      for clock in _setting(config, 'clock_times', ())]
        return min((x for x in candidates if x > at and trading.allows(x.time())), default=None)

    planned = {}
    for row in data['rows']:
        trade, order = by_trade[row['trade_id']], by_order[row['order_id']]
        opened = timestamp(trade['opened_at'])
        etf = trade.get('etf_symbol') or _setting(config, 'etf_codes')[0]
        domestic = 'SH' if str(etf).startswith('5') else 'SZ'
        same_market = order['symbol'].endswith('.' + domestic)
        if trade['side'] == 'CREATE' and (order['purpose'] == 'ENTRY' or same_market):
            if one_minute and trade.get('applied_at'):
                at = timestamp(trade['applied_at'])
            else:
                at = opened if signal_entry else next_investor_minute(opened)
            basis = 'original_domestic_creation_buy_before_cash_substitution'
        elif order['purpose'] == 'MANAGER' and not split:
            raw = order.get('attempt_scheduled_at', legs.get(row['trade_id'], {}).get('attempt_scheduled_at'))
            at = timestamp(raw) if raw else None
            basis = 'configured_legacy_manager_attempt'
        else:
            raw = trade.get('applied_at') or order.get('submitted')
            at = timestamp(raw) if raw else None
            if at is not None and not split:
                at = next_investor_minute(at)
            basis = 'application_minute_stock_buy_or_sell' if split else 'next_investor_minute_after_application'
        planned[row['order_id']] = at, basis
    references, requests = {}, defaultdict(set)
    wanted = {(r['trade_id'], r['symbol'], planned[r['order_id']][0])
              for r in data['rows'] if planned[r['order_id']][0] is not None}

    def remember(key, at, raw, source):
        if at in (None, '') or raw in (None, ''):
            return
        at = timestamp(at)
        key = (*key, at)
        if key not in wanted:
            return
        price = decimal(raw)
        if price <= ZERO:
            raise ValueError(f'Invalid planned-minute reference: {key} {at}')
        old = references.get(key)
        if old is not None and old[0] == at and old[1] != price:
            raise ValueError(f'Conflicting planned-minute reference: {key} {at}')
        if old is None or at > old[0]:
            references[key] = at, price, source

    for row in substitutions:
        remember((row['trade_id'], row['symbol']), row.get('ts'), row.get('observed_price', row.get('price')),
                 'saved_creation_substitution_signal_price')
    for row in stock_rows:
        remember((row['trade_id'], row['symbol']), row.get('signal_price_at'),
                 row.get('signal_price'), 'saved_stock_signal_price')
    for row in data['rows']:
        remember((row['trade_id'], row['symbol']), row['first_blocked_at'],
                 row.get('blocked_price', row['initial_price']), 'block_record_at_planned_minute')
    for key in wanted - references.keys():
        requests[key[2]].add(key[1])
    prices = EventPrices(_setting(config, 'minute_root'))
    prices.read(requests)
    for key in wanted - references.keys():
        bar = prices.get(key[1], key[2], exact=True)
        if bar:
            remember(key[:2], bar.time, bar.close, 'exact_planned_minute_close')

    fill_map, pricing_map = defaultdict(list), defaultdict(list)
    for row in fills:
        fill_map[row['order_id']].append(row)
    for row in pricing:
        pricing_map[row['trade_id'], row['symbol']].append(row)
    rows = []
    for original in data['rows']:
        row = dict(original)
        order = by_order[row['order_id']]
        opened, horizon = timestamp(row['opened_at']), timestamp(row['observed_through'])
        blocked_at = timestamp(row['first_blocked_at'])
        planned_at, planned_basis = planned[row['order_id']]
        quantity = _quantity(order['quantity'])
        selected = [r for r in fill_map[row['order_id']] if opened <= timestamp(r['ts']) <= horizon]
        values = ([r for r in pricing_map[row['trade_id'], row['symbol']]
                   if opened <= timestamp(r['ts']) <= horizon] if order['purpose'] == 'MANAGER' else [])
        filled = sum(_quantity(r['quantity']) for r in selected)
        valued = sum(_quantity(r['quantity']) for r in values)
        remaining = quantity-filled-valued
        if remaining < 0:
            raise ValueError(f'Full-cycle quantities counted twice: {row["order_id"]}')
        reference_at, reference, reference_source = references.get(
            (row['trade_id'], row['symbol'], planned_at), (None, None, 'missing_exact_planned_minute_price'))
        fill_value = sum((decimal(r['price'])*_quantity(r['quantity']) for r in selected), ZERO)
        valuation_value = sum((decimal(r['price'])*_quantity(r['quantity']) for r in values), ZERO)
        sign = -1 if order['side'] == 'BUY' else 1

        def change(amount, shares):
            return ZERO if not shares else None if reference is None or amount is None else sign*(amount-shares*reference)

        first_fill = min((timestamp(r['ts']) for r in selected), default=None)
        last_fill = max((timestamp(r['ts']) for r in selected), default=None)
        valued_at = max((timestamp(r['ts']) for r in values), default=None)
        end = horizon if remaining else max(at for at in (last_fill, valued_at) if at is not None)
        mark = decimal(row.get('mark_price'))
        row.update(blocked_price=decimal(original.get('blocked_price', original['initial_price'])),
            blocked_quantity=_quantity(original.get('blocked_quantity', original['initial_quantity'])),
            blocked_elapsed_minutes=decimal(original.get('blocked_elapsed_minutes', original['elapsed_minutes'])),
            planned_at=planned_at, planned_timing_basis=planned_basis,
            initial_price=reference, initial_price_at=reference_at, initial_price_source=reference_source,
            initial_quantity=quantity, initial_exposure=quantity*reference if reference is not None else None,
            filled_quantity=filled,
            filled_after_block=sum(_quantity(r['quantity']) for r in selected if timestamp(r['ts']) >= blocked_at),
            valued_quantity=valued, untraded_quantity=quantity-filled, unpriced_quantity=remaining,
            first_fill_at=first_fill, last_fill_at=last_fill,
            fill_price=fill_value/filled if filled else None,
            valuation_at=valued_at, valuation_price=valuation_value/valued if valued else None,
            exposure_end_at=end, elapsed_minutes=Decimal(str((end-planned_at).total_seconds()))/60 if planned_at else None,
            cross_day=end.date() > planned_at.date() if planned_at else False,
            status='未锁价' if remaining else '期限估值锁价（非成交）' if valued else '已模拟成交',
            remaining_exposure=ZERO if not remaining else remaining*mark if mark is not None else None,
            realized_price_pnl=change(fill_value, filled),
            valuation_price_pnl=change(valuation_value, valued),
            unrealized_price_pnl=change(remaining*mark if mark is not None else None, remaining))
        rows.append(row)
    return price_only_limit_exposure(dict(data, rows=rows, schema_version=5, pnl_basis=PLANNED_EXECUTION_BASIS,
        domestic_creation_at_signal=bool(signal_entry), market_split_execution=bool(split),
        quantity_basis='whole_selected_order_including_fills_before_first_block',
        reference_price_basis='exact_original_planned_stock_buy_or_sell_minute_no_fallback',
        duration_basis='planned_stock_execution_to_final_fill_valuation_or_observation_horizon',
        blocked_duration_basis='first_recorded_block_to_final_fill_valuation_or_observation_horizon'))


def _initial_substitution_blocks(config, trades, orders, substitutions, rules, *, horizon, include_followup=True):
    """A signal-time optional substitution qualifies only if t+1 still hits upper limit.

    The manager is scheduled for a later day; that scheduled waiting period is
    exposure duration, not evidence of a continuously locked limit order book.
    """
    by_trade = {r['trade_id']: r for r in trades}
    order_map = {(r['trade_id'], r['symbol']): r for r in orders if r['purpose'] == 'MANAGER' and r['side'] == 'BUY'}
    candidates = [r for r in substitutions if str(r.get('reason', '')).removeprefix('stock_').startswith('limit_up:')
                  and (r['trade_id'], r['symbol']) in order_map]
    if not candidates:
        return []
    repository = FilePCFRepository(_setting(config, 'pcf_root'))
    requests, contexts = defaultdict(set), {}
    for row in candidates:
        trade = by_trade[row['trade_id']]
        at = timestamp(trade['opened_at'])
        if at + timedelta(minutes=1) > horizon:
            continue
        code = (trade.get('etf_symbol') or _setting(config, 'etf_codes')[0]).split('.')[0]
        try:
            pcf = repository.get(code, at.date())
        except FileNotFoundError:
            continue
        contexts[row['trade_id']] = pcf
        requests[at + timedelta(minutes=1)].add(row['symbol'])
        requests[datetime.combine(at.date(), time(9, 30))].add(row['symbol'])
        if pcf.pre_trading_day and pcf.pre_trading_day < at.date():
            requests[datetime.combine(pcf.pre_trading_day, time(15))].add(row['symbol'])
    prices = EventPrices(_setting(config, 'minute_root'))
    prices.read(requests)
    limits = ReportPriceLimits.from_config(prices, config, event_prices=prices)
    result = []
    for row in candidates:
        trade, order = by_trade[row['trade_id']], order_map[row['trade_id'], row['symbol']]
        if row['trade_id'] not in contexts:
            continue
        at = timestamp(trade['opened_at'])
        next_at = at + timedelta(minutes=1)
        limits.on_day_open(contexts[row['trade_id']], rules)
        reason = limits.status(row['symbol'], next_at)
        if not reason or not reason.startswith('limit_up:'):
            continue
        observations = [(at, row.get('observed_price', row['price']), row['reason'])]
        if include_followup:
            observations.append((next_at, prices.get(row['symbol'], next_at).close, reason))
        for when, price, cause in observations:
            result.append(dict(ts=when, trade_id=row['trade_id'], order_id=order['order_id'],
                symbol=row['symbol'], side='BUY', purpose='MANAGER',
                unfilled_quantity=row['quantity'], close=price, reason=cause))
    return result


def build_limit_exposure(config, trades, orders, fills, fees, pricing, blocks, signals,
                         *, substitutions=(), settlements=(), rules=None,
                         observed_through=None, records_available=True, stock_rows=(),
                         partial_records=False):
    if observed_through is None:
        day = _setting(config, 'settlement_end_date') or _setting(config, 'end_date')
        observed_through = datetime.fromisoformat(str(day)).replace(hour=15)
    horizon = timestamp(observed_through)
    data = dict(schema_version=2, selection_basis='recorded_directional_limit_delay',
                adjusted_references_sha256=AdjustedReferences.from_config(config).sha256,
                stock_suspensions_sha256=StockSuspensions.from_config(config).sha256,
                chronological_order='opened_at_then_first_block_then_symbol',
                records_available=records_available, observed_through=horizon, rows=[],
                duration_basis='elapsed_clock_minutes_including_lunch_overnight_and_scheduled_wait')
    if not records_available:
        return planned_execution_limit_exposure(data, config, trades, orders, fills, pricing,
                                               settlements=settlements)
    by_trade = {r['trade_id']: r for r in trades}
    by_order = {r['order_id']: r for r in orders}
    if rules is None:
        path = _setting(config, 'rules_path')
        rules = load_rules(path) if path else InMemoryRuleRepository()
    rules = with_suspensions(config, rules)
    events = list(blocks) + _initial_substitution_blocks(
        config, trades, orders, substitutions, rules, horizon=horizon, include_followup=not partial_records)
    if partial_records:
        data['block_coverage'] = 'partial_saved_observations'
    grouped, fill_map, pricing_map = (defaultdict(list) for _ in range(3))
    for event in events:
        trade = by_trade.get(event['trade_id'])
        if trade is None or event.get('purpose') not in {'ENTRY', 'EXIT', 'MANAGER'}:
            continue
        if timestamp(event['ts']) > horizon:
            continue
        side = 'BUY' if trade['side'] == 'CREATE' else 'SELL'
        reason = 'limit_up:' if side == 'BUY' else 'limit_down:'
        if event['side'] == side and str(event['reason']).removeprefix('stock_').startswith(reason):
            grouped[event['order_id']].append(event)
    for row in fills:
        fill_map[row['order_id']].append(row)
    # The fees argument remains for call compatibility; this analysis excludes it.
    for row in pricing:
        pricing_map[row['trade_id'], row['symbol']].append(row)
    signal_map = {timestamp(r['ts']): r for r in signals}
    leg_map = {r['trade_id']: r for r in settlements if r['kind'] == 'cash_substitution'}
    names = {(r['trade_id'], r['symbol']): r.get('security_name', '') for r in stock_rows}
    mark_requests = defaultdict(set)
    for oid, entries in grouped.items():
        order = by_order.get(oid)
        if order is None:
            raise ValueError(f'Limit exposure has no matching order: {oid}')
        entries.sort(key=lambda r: timestamp(r['ts']))
        first, trade = entries[0], by_trade[order['trade_id']]
        start = timestamp(first['ts'])
        initial_qty, p0 = _quantity(first['unfilled_quantity']), decimal(first['close'])
        if initial_qty == 0 or (p0 is None and not partial_records) or (p0 is not None and p0 <= 0):
            raise ValueError(f'Limit exposure lacks quantity/price: {oid}')
        selected = sorted((r for r in fill_map[oid] if start <= timestamp(r['ts']) <= horizon),
                          key=lambda r: timestamp(r['ts']))
        values = ([r for r in pricing_map[order['trade_id'], order['symbol']]
                   if start <= timestamp(r['ts']) <= horizon] if order['purpose'] == 'MANAGER' else [])
        filled = sum(_quantity(r['quantity']) for r in selected)
        valued = sum(_quantity(r['quantity']) for r in values)
        if filled + valued > initial_qty:
            raise ValueError(f'Exposure quantities counted twice: {oid}')
        remaining = initial_qty - filled - valued
        last_fill = max((timestamp(r['ts']) for r in selected), default=None)
        valued_at = max((timestamp(r['ts']) for r in values), default=None)
        end = horizon if remaining else max(x for x in (last_fill, valued_at) if x is not None)
        elapsed = Decimal(str((end - start).total_seconds())) / 60
        if elapsed < 1 and not remaining:
            continue
        sign = -1 if order['side'] == 'BUY' else 1
        filled_value = sum((decimal(r['price']) * _quantity(r['quantity']) for r in selected), ZERO)
        valued_value = sum((decimal(r['price']) * _quantity(r['quantity']) for r in values), ZERO)
        shares = decimal(trade.get('etf_shares'))
        iopv = decimal(signal_map.get(timestamp(trade['opened_at']), {}).get('iopv'))
        basket = shares * iopv if shares is not None and iopv is not None and shares > 0 and iopv > 0 else None
        row = dict(trade_id=trade['trade_id'], order_id=oid, opened_at=timestamp(trade['opened_at']),
            side=trade['side'], symbol=order['symbol'], security_name=names.get((trade['trade_id'], order['symbol']), ''),
            initial_limit_state=str(first['reason']).split(':', 1)[0].removeprefix('stock_'),
            purpose=order['purpose'], exposure_type=('待代买缺券' if order['side'] == 'BUY' else
                '待代理卖出' if order['purpose'] == 'MANAGER' else '已赎回待卖持仓'),
            first_blocked_at=start, last_limit_blocked_at=max(timestamp(r['ts']) for r in entries),
            observed_limit_minutes=len({timestamp(r['ts']) for r in entries}), exposure_end_at=end,
            observed_through=horizon, elapsed_minutes=elapsed, cross_day=end.date() > start.date(),
            status='未锁价' if remaining else '期限估值锁价（非成交）' if valued else '已模拟成交',
            initial_quantity=initial_qty, filled_after_block=filled, valued_quantity=valued,
            untraded_quantity=initial_qty-filled, unpriced_quantity=remaining,
            initial_price=p0, initial_exposure=initial_qty*p0 if p0 is not None else None,
            first_fill_at=min((timestamp(r['ts']) for r in selected), default=None), last_fill_at=last_fill,
            fill_price=filled_value/filled if filled else None, valuation_at=valued_at,
            valuation_price=valued_value/valued if valued else None,
            mark_at=None, mark_price=None, remaining_exposure=ZERO if not remaining else None,
            realized_price_pnl=sign*(filled_value-filled*p0) if p0 is not None else None,
            valuation_price_pnl=sign*(valued_value-valued*p0) if p0 is not None else None,
            unrealized_price_pnl=ZERO if not remaining else None,
            etf_shares=shares, basket_value=basket,
            paid_at=leg_map.get(trade['trade_id'], {}).get('paid_at') if order['purpose'] == 'MANAGER' else None,
            limit_sources=';'.join(sorted({str(r['reason']).split(':', 1)[-1] for r in entries})))
        if remaining:
            mark_requests[horizon].add(order['symbol'])
        data['rows'].append(row)
    marks = EventPrices(_setting(config, 'minute_root'))
    marks.read(mark_requests)
    pcf_root = _setting(config, 'pcf_root')
    repository = FilePCFRepository(pcf_root) if pcf_root else None
    for row in data['rows']:
        if not row['security_name'] and repository is not None:
            trade = by_trade[row['trade_id']]
            code = (trade.get('etf_symbol') or _setting(config, 'etf_codes')[0]).split('.')[0]
            try:
                pcf = repository.get(code, row['opened_at'].date())
                row['security_name'] = next((c.security_name or '' for c in pcf.components
                    if not c.is_cash_placeholder and component_symbol(c) == row['symbol']), '')
            except FileNotFoundError:
                pass  # Legacy report names can be absent; never invent a security name.
        if row['unpriced_quantity']:
            mark = marks.get(row['symbol'], horizon)
            if mark:
                row.update(mark_at=mark.time, mark_price=mark.close,
                    remaining_exposure=row['unpriced_quantity'] * mark.close,
                    unrealized_price_pnl=(1 if row['side'] == 'REDEEM' else -1)
                        * row['unpriced_quantity'] * (mark.close-row['initial_price']) if row['initial_price'] is not None else None)
    data['rows'].sort(key=lambda r: (r['opened_at'], r['first_blocked_at'], r['symbol'], r['order_id']))
    return planned_execution_limit_exposure(data, config, trades, orders, fills, pricing,
                                    substitutions=substitutions, stock_rows=stock_rows, settlements=settlements)


def build_from_saved_records(config, trades, orders, fills, fees, pricing, signals, *,
                             substitutions=(), attempts=(), settlements=(), execution_blocks=None,
                             redemption_risks=(), redemption_sell_blocks=(), stock_rows=(), rules=None, observed_through=None):
    """Use the execution log when available, or only surviving dated, directional evidence.

    Some versions kept cash-proxy attempts and application-minute reasons but
    did not log every stock execution check. A late fill or a touched signal
    alone cannot identify a failed limit order. An empty complete log differs
    from an absent one.
    """
    partial = execution_blocks is None
    if partial:
        by_trade = {trade['trade_id']: trade for trade in trades}
        orders_by_key = {(o['trade_id'], o['symbol']): o for o in orders
                         if o['purpose'] in ('MANAGER', 'EXIT')}
        orders_by_id = {o['order_id']: o for o in orders}
        observations = {}
        requests = defaultdict(set)

        def record(order, at, reason, quantity, price=None):
            if order is None or not at or quantity in (None, ''):
                return
            trade = by_trade.get(order['trade_id'])
            if trade is None:
                return
            expected = 'limit_up:' if trade['side'] == 'CREATE' else 'limit_down:'
            if not str(reason).removeprefix('stock_').startswith(expected) or _quantity(quantity) == 0:
                return
            at = timestamp(at)
            key = (order['order_id'], at)
            if key not in observations:
                observations[key] = dict(trade_id=order['trade_id'], order_id=order['order_id'],
                    ts=at, symbol=order['symbol'], purpose=order['purpose'], side=order['side'],
                    reason=reason, unfilled_quantity=quantity, close=price)
            if observations[key]['close'] in (None, ''):
                if price not in (None, ''):
                    observations[key]['close'] = price
                else:
                    requests[at].add(order['symbol'])

        for block in redemption_sell_blocks:
            record(orders_by_id.get(block['order_id']), block['ts'], block['reason'],
                   block['unfilled_quantity'], block['close'])
        for attempt in attempts:
            unfilled = _quantity(attempt.get('unfilled_quantity') or 0)
            if unfilled:
                requested = attempt.get('requested_quantity')
                if requested in (None, ''):
                    requested = unfilled + _quantity(attempt.get('filled_quantity') or 0)
                record(orders_by_id.get(attempt.get('order_id')), attempt.get('ts'),
                       attempt.get('reason'), requested)
        for risk in redemption_risks:
            at = risk.get('entry_at')
            exact = at and risk.get('reference_at') and timestamp(at) == timestamp(risk['reference_at'])
            record(orders_by_key.get((risk['trade_id'], risk['symbol'])), at, risk.get('reason'),
                   risk.get('quantity'), risk.get('reference_price') if exact else None)
        for trade in trades:
            if trade['side'] != 'REDEEM' or not trade.get('applied_at'):
                continue
            delayed = trade.get('delayed_redemption_components')
            if isinstance(delayed, str) and delayed.lstrip().startswith('{'):
                delayed = json.loads(delayed)
            if isinstance(delayed, dict):
                for symbol, reason in delayed.items():
                    order = orders_by_key.get((trade['trade_id'], symbol))
                    if order:
                        record(order, trade['applied_at'], reason, order['quantity'])
        if requests:
            prices = EventPrices(_setting(config, 'minute_root'))
            prices.read(requests)
            for event in observations.values():
                if event['close'] in (None, ''):
                    bar = prices.get(event['symbol'], event['ts'], exact=True)
                    event['close'] = bar.close if bar else None
        execution_blocks = observations.values()
    return build_limit_exposure(config, trades, orders, fills, fees, pricing,
        execution_blocks, signals, substitutions=substitutions, settlements=settlements,
        rules=rules, observed_through=observed_through, stock_rows=stock_rows, partial_records=partial)


def save_limit_exposure(output, data):
    data = price_only_limit_exposure(data)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'limit_exposure.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(data['rows'])
    (output / 'limit_exposure.json').write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    main_report = 'report.html' if output.name == 'report' or (output / 'report.html').is_file() else 'report/report.html'
    page = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>涨跌停纯价格影响与敞口</title>'
            '<style>body{font-family:system-ui,sans-serif;margin:24px;color:#20252b;line-height:1.6}'
            'table{border-collapse:collapse}th,td{border:1px solid #ddd}th{background:#f4f6f8}</style>'
            f'<p><a href="{main_report}">完整回测报告</a></p>' + limit_exposure_html(data) + '</html>')
    (output / 'limit_exposure.html').write_text(page, encoding='utf-8')


def limit_exposure_html(data, *, id_suffix=''):
    data = price_only_limit_exposure(data)
    suffix = '-' + quote(id_suffix, safe='') if id_suffix else ''
    rows = data['rows']
    full_cycle = data.get('pnl_basis') in {FULL_CYCLE_BASIS, PLANNED_EXECUTION_BASIS}
    planned_period = data.get('pnl_basis') == PLANNED_EXECUTION_BASIS
    reference_label = '计划买卖' if planned_period else '发起套利'
    title = '<h2>涨跌停延迟成交与敞口（不含税费，按发起时间排列）</h2>'
    if not data.get('records_available', True):
        return title + '<p>旧运行缺少成交受阻记录，无法据此判断延迟敞口；需要按保守成交规则重新回测。</p>'
    def text(v):
        return escape(str(v)) if v not in (None, '') else '未确定'
    def number(v, digits=2):
        return '未确定' if v in (None, '') else f'{decimal(v):,.{digits}f}'
    def pct(v):
        return '未确定' if v in (None, '') else f'{decimal(v):.6%}'
    cross = sum(bool(r['cross_day']) for r in rows)
    pending = sum(int(r['unpriced_quantity']) > 0 for r in rows)
    long = sum(decimal(r['elapsed_minutes']) is not None and decimal(r['elapsed_minutes']) >= 5 for r in rows)
    doc = title
    if data.get('block_coverage') == 'partial_saved_observations':
        doc += ('<p><strong>受阻记录覆盖不完整。</strong>本表按已保存的逐笔代买卖尝试、申赎时的逐股受阻原因及现金替代记录重建。'
                '缺少完整执行流水的订单无法判定，未列入的交易仍可能有其他受阻。</p>')
    doc += (f'<p>纳入 {len(rows)} 条受阻敞口；其中等待至少 5 分钟 {long} 条、跨日 {cross} 条、'
        f'截至 {text(data["observed_through"])} 仍未锁价 {pending} 条。所有时长均保留，按发起时间从早到晚排列。</p>'
        '<p>依据实际记录的申购涨停 BUY 受阻、赎回跌停 SELL 受阻筛选；信号触价后立即正常成交的股票不纳入。'
        '申购本市场现金替代需下一分钟仍触及涨停，或后续定时代买实际受阻，才纳入。'
        '首次受阻状态标明该股在首次确认受阻时是涨停还是跌停。价格影响为零的受阻敞口仍保留。'
        '来源 estimated_open_0930_proxy 表示缺少原可算限价时按当日 09:30 精确 close 作开盘代理估算；'
        '这不是真实开盘价或交易所限价，缺少该分钟价格仍为未确定。</p>' +
        (f'<p>期间时长从{reference_label}分钟至该股全部成交、期限估值定价或本轮观察截止；首次受阻价仅供参考，不作为期间盈亏基准。'
         if full_cycle else '<p>等待时长从首次受阻至全部成交、代理期限估值锁价或本轮末日，包含午休、隔夜及原定时补券等待；') +
        '确认受阻分钟数仅统计有记录的限价分钟，不表示整个等待区间持续封板。'
        '期限估值不是成交，也不是到账；未锁价价格影响属于浮动估值。</p>'
        + (f'<p>期间纯价格影响（不含税费）：申购＝（{reference_label}分钟参考价－最终成交或估值价）×股数；'
           f'赎回＝（最终成交或估值价－{reference_label}分钟参考价）×股数。纳入该股票订单的全部数量，包括首次受阻前已成交的部分。'
           + ((('申购本市场原计划买入使用 t 分钟，即使后来转现金替代仍从 t 算。' if data.get('domestic_creation_at_signal', True)
                else '申购本市场使用该轮原定的信号后买入分钟。')
               + ('跨市场代买代卖及赎回实物股票使用实际申赎提交分钟。' if data.get('market_split_execution', True)
                  else '其余订单以该轮原定的代理尝试或申赎后的可交易分钟为基准。') +
              '只用计划分钟的精确价格，缺失保持未确定，不用 t−1 或后续价格替代。'
              ) if planned_period else '发起参考价采用当时已记录的价格，或同日 t／t−1 分钟行情。')
           if full_cycle else '<p>价格变动盈亏只按价差计算：申购＝（首次受阻价－后续定价）×股数；赎回＝（后续定价－首次受阻价）×股数。') +
        '分批成交、期限估值及未锁价浮动部分分别计算后合计。'
        'ETF 成本影响取该盈亏的相反数；每份金额除以 ETF 份数，比例除以信号整篮估值。'
        '这些金额仅解释价格变化，不是涨跌停的独立因果损失，不再计入账户或重复扣减总盈亏；不同时间的敞口金额不能相加当作同一时点总敞口。</p>'
        '<p><a href="limit_exposure.csv">下载全部延迟敞口 CSV</a></p>')
    table_id = 'limit-exposure-table' + suffix
    doc += (f'<style>#{table_id}{{width:100%;min-width:0;table-layout:fixed;font-size:12px}}'
            f'#{table_id} td,#{table_id} th{{white-space:normal;overflow-wrap:anywhere;vertical-align:top;padding:8px 5px}}</style>'
            f'<table id="{table_id}"><thead><tr>')
    headings = ('交易／发起时间', '股票／方向', '首次受阻状态',
        '成交或估值时刻／观察截至', f'{reference_label}至定价时长／确认受阻' if full_cycle else '等待时长／确认受阻',
        '股数：全部／成交／估值／未锁价' if full_cycle else '股数：起始／成交／估值／未锁价',
        f'价格：{reference_label}／受阻／成交／估值' if full_cycle else '价格：受阻／成交均价／估值或期末',
        '期间纯价格影响（不含税费）' if full_cycle else '价格变动盈亏合计（元）',
        '期间价格变动对 ETF 成本影响／每份／比例' if full_cycle else '价格变动对 ETF 成本影响／每份／比例')
    doc += ''.join(f'<th>{h}</th>' for h in headings) + '</tr></thead><tbody>'
    for r in rows:
        values = [f'{text(r["trade_id"])}<br>{text(r["opened_at"])}',
            f'{text(r["symbol"])} {text(r["security_name"] or "")}<br>{"申购" if r["side"] == "CREATE" else "赎回"}',
            {'limit_up': '涨停', 'limit_down': '跌停'}.get(r.get('initial_limit_state'), '未确定')
                + ('<br>09:30 close 开盘代理估算' if 'estimated_open_0930_proxy'
                   in r.get('limit_sources', '').split(';') else ''),
            text(r['exposure_end_at']),
            f'{number(r["elapsed_minutes"], 0)} 分钟{"（跨日）" if r["cross_day"] else ""}<br>{r["observed_limit_minutes"]} 个确认受阻分钟',
            ' / '.join(number(r[k], 0) for k in ('initial_quantity', 'filled_quantity' if full_cycle else 'filled_after_block', 'valued_quantity', 'unpriced_quantity')),
            (f'{reference_label}参考 {number(r["initial_price"], 4)}<br>计划时刻 {text(r.get("planned_at", r.get("initial_price_at")))}<br>价格时刻 {text(r.get("initial_price_at"))}<br>'
             f'首次受阻 {number(r.get("blocked_price"), 4)}<br>成交 {number(r["fill_price"], 4)}'
             if full_cycle else f'{number(r["initial_price"], 4)} / {number(r["fill_price"], 4)}')
                + f'<br>期限估值 {number(r["valuation_price"], 4)}；期末 {number(r["mark_price"], 4)}',
            f'{number(r["period_price_pnl"])} 元',
            f'{number(r["etf_cost_impact"])} 元<br>{number(r["etf_cost_impact_per_share"], 8)} 元/份<br>{pct(r["etf_cost_impact_ratio"])}']
        doc += '<tr>' + ''.join(f'<td>{v}</td>' for v in values) + '</tr>'
    if not rows:
        doc += f'<tr><td colspan="{len(headings)}">本轮记录未识别到符合条件的涨跌停延迟敞口；不代表实盘没有流动性风险。</td></tr>'
    return doc + '</tbody></table>'
