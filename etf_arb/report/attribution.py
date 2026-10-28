"""Per-trade reporting attribution; never changes execution or cash balances."""

from collections import defaultdict
import csv
from datetime import datetime
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.parse import quote

from etf_arb.pricing.iopv import SubstitutionKind, _normalize_substitution_flag

ZERO = Decimal('0')
SIGNAL_BASIS_FIELDS = ('signal_physical_value', 'signal_manager_value', 'signal_fixed_cash',
                       'signal_physical_quantity')
REPORT_FIELDS = ('trade_id', 'opened_at', 'side', 'net_pnl', 'signal_premium_rate',
                 'signal_gross_edge', 'cash_substitution_pnl', 'estimated_cash_pnl',
                 'cash_substitution_cash_amount', 'estimated_cash', 'actual_cash',
                 'etf_price_effect', 'physical_price_effect', 'investor_fees', 'manager_fees',
                 'cash_substitution_valued_quantity')


def decimal(value):
    if value is None or value == '':
        return None
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('Non-finite report amount')
    return result


def timestamp(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def signal_reference(plan, pcf, units, price_at):
    """Capture only prices already visible at the original signal timestamp."""
    def value(quantities):
        values = []
        for symbol, quantity in quantities.items():
            price = price_at(symbol)
            values.append(None if price is None else quantity * price)
        return None if any(x is None for x in values) else sum(values, ZERO)
    mandatory = [c for c in pcf.components if not c.is_cash_placeholder
                 and _normalize_substitution_flag(c.substitution_flag) is SubstitutionKind.MANDATORY]
    fixed = [c.creation_cash_substitute for c in mandatory]
    return dict(signal_physical_value=value(plan.physical), signal_manager_value=value(plan.manager),
                signal_fixed_cash=None if any(x is None for x in fixed) else sum(fixed, ZERO) * units,
                signal_physical_quantity=sum(plan.physical.values()))


def leg_status(leg):
    if not leg:
        return '未申请'
    if decimal(leg.get('final')) is None:
        return '未定价'
    return '已交收' if leg.get('paid') in (True, 'True', 'true') else '已定价待交收'


def build_trade_attribution(trades, signals, fills, settlements, fees, pricing_rows=()):
    signal_by_time = {timestamp(r['ts']): r for r in signals}
    fill_by_id, fee_by_id, pricing_by_id = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in fills:
        fill_by_id[row['trade_id']].append(row)
    for row in fees:
        fee_by_id[row['trade_id']].append(row)
    for row in pricing_rows:
        pricing_by_id[row['trade_id']].append(row)
    legs = {(r['trade_id'], r['kind']): r for r in settlements}
    result = []
    for trade in trades:
        tid, side = trade['trade_id'], trade['side']
        sign = Decimal('-1') if side == 'CREATE' else Decimal('1')
        signal = signal_by_time.get(timestamp(trade['opened_at']), {})
        shares = int(trade['etf_shares'])
        etf_price, iopv = decimal(signal.get('etf_close')), decimal(signal.get('iopv'))
        premium = decimal(signal.get('premium'))
        edge = decimal(trade.get('signal_gross_edge'))
        if edge is None and etf_price is not None and iopv is not None:
            edge = abs(etf_price - iopv) * shares
        etf_fills = [f for f in fill_by_id[tid] if f['symbol'] == '159338.SZ' and f['purpose'] != 'MANAGER']
        physical_fills = [f for f in fill_by_id[tid] if f['symbol'] != '159338.SZ' and f['purpose'] != 'MANAGER']
        etf_value = sum((decimal(f['price']) * int(f['quantity']) for f in etf_fills), ZERO)
        physical_value = sum((decimal(f['price']) * int(f['quantity']) for f in physical_fills), ZERO)
        physical_reference = decimal(trade.get('signal_physical_value'))
        manager_reference = decimal(trade.get('signal_manager_value'))
        fixed_reference = decimal(trade.get('signal_fixed_cash'))
        physical_complete = (trade.get('signal_physical_quantity') not in (None, '') and
                             sum(int(f['quantity']) for f in physical_fills) == int(trade['signal_physical_quantity']))
        etf_effect = (-sign * (etf_value - etf_price * shares)
                      if etf_price is not None and sum(int(f['quantity']) for f in etf_fills) == shares else None)
        physical_effect = sign * (physical_value - physical_reference) if physical_complete and physical_reference is not None else None
        manager_fees = sum((decimal(f['total']) for f in fee_by_id[tid]
                            if f.get('charged_to') == 'cash_substitution' or f.get('purpose') == 'MANAGER'), ZERO)
        total_fees = sum((decimal(f['total']) for f in fee_by_id[tid]), ZERO)
        investor_fees = total_fees - manager_fees
        cash_leg = legs.get((tid, 'cash_difference'), {})
        sub_leg = legs.get((tid, 'cash_substitution'), {})
        sub_final = decimal(sub_leg.get('final'))
        deposit = decimal(trade['substitution_deposit'])
        fixed = decimal(trade['domestic_fixed_cash'])
        sub_actual = (deposit - sub_final + fixed if side == 'CREATE' else sub_final) if sub_final is not None else None
        sub_reference = manager_reference + fixed_reference if manager_reference is not None and fixed_reference is not None else None
        sub_pnl = sign * (sub_actual - sub_reference) if sub_actual is not None and sub_reference is not None else None
        estimated = decimal(trade['estimated_cash'])
        actual = decimal(cash_leg.get('actual_component'))
        cash_final = decimal(cash_leg.get('final'))
        if actual is None and cash_final is not None:
            # Current main's legacy positive-estimate deposit is refunded once.
            actual = max(estimated, ZERO) - cash_final if side == 'CREATE' else cash_final - max(estimated, ZERO)
        cash_pnl = sign * (actual - estimated) if actual is not None else None
        pnl = decimal(trade.get('net_pnl')) if trade['state'] == 'CLOSED' else None
        components = (edge, etf_effect, physical_effect, sub_pnl, cash_pnl)
        residual = None
        if pnl is not None and all(value is not None for value in components):
            residual = pnl - (sum(components, ZERO) - investor_fees)
            if abs(residual) > Decimal('0.000001'):
                raise ValueError(f'Trade report attribution does not reconcile: {tid}: {residual}')
        result.append(dict(trade_id=tid, opened_at=trade['opened_at'], applied_at=trade.get('applied_at'),
            settled_at=trade.get('settled_at'), side=side, state=trade['state'], etf_shares=shares,
            signal_etf_price=etf_price, signal_iopv=iopv, signal_premium_rate=premium,
            signal_spread_per_share=etf_price-iopv if etf_price is not None and iopv is not None else None,
            signal_gross_edge=edge, net_pnl=pnl, etf_price_effect=etf_effect,
            physical_price_effect=physical_effect, cash_substitution_signal_basis=sub_reference,
            cash_substitution_actual_value=sub_actual, cash_substitution_pnl=sub_pnl,
            cash_substitution_cash_amount=sub_final, cash_substitution_cash_estimate=decimal(sub_leg.get('estimate')),
            cash_substitution_deposit=deposit, cash_substitution_status=leg_status(sub_leg),
            cash_substitution_priced_at=sub_leg.get('priced_at'), cash_substitution_paid_at=sub_leg.get('paid_at'),
            cash_substitution_valued_quantity=sum(int(r['quantity']) for r in pricing_by_id[tid]),
            estimated_cash=estimated, actual_cash=actual, estimated_cash_pnl=cash_pnl,
            cash_difference_cash_amount=cash_final, cash_difference_status=leg_status(cash_leg),
            cash_difference_known_on=cash_leg.get('known_on'), cash_difference_paid_at=cash_leg.get('paid_at'),
            investor_fees=investor_fees, manager_fees=manager_fees, total_fees=total_fees,
            attribution_residual=residual,
            attribution_status='已核对' if residual is not None else '待结清' if pnl is None else '缺信号基准'))
    return result


def save_attribution_csv(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_FIELDS, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    return path


def display_row(row):
    def amount(name, digits=2):
        value = decimal(row.get(name))
        return '未确定' if value is None else f'{value:,.{digits}f}'
    premium = decimal(row.get('signal_premium_rate'))
    return {'交易编号': row['trade_id'], '发起时间': row['opened_at'],
        '方向': '申购' if row['side'] == 'CREATE' else '赎回',
        '最终净盈亏（元）': amount('net_pnl'),
        '信号折溢价率': '未确定' if premium is None else f'{premium:+.4%}',
        '信号毛价差（元）': amount('signal_gross_edge'),
        '现金替代价格盈亏（元，含代理费）': amount('cash_substitution_pnl'),
        '预估现金盈亏影响（元）': amount('estimated_cash_pnl'),
        '现金替代退补／应收款（元，非利润）': amount('cash_substitution_cash_amount'),
        '预估现金E（元）': amount('estimated_cash'), '实际现金A（元）': amount('actual_cash'),
        'ETF价格变化影响（元）': amount('etf_price_effect'),
        '实物股票价格影响（元）': amount('physical_price_effect'),
        '投资者税费（元）': amount('investor_fees'), '代理税费（元，已含在替代盈亏）': amount('manager_fees'),
        '未成交估值数量': row['cash_substitution_valued_quantity']}


def attribution_html(rows):
    if not rows:
        return '<h2>逐笔价差与现金盈亏</h2><p>本轮没有已发起交易。</p>'
    shown = [display_row(r) for r in rows]
    columns = list(shown[0])
    document = '''<h2 id="trade-attribution">逐笔价差与现金盈亏</h2>
<p>现金替代价格盈亏＝相对信号估值的价格变化，已包含代理税费；退补款／应收款是资金收付，不等于利润。
预估现金影响：申购 E−A、赎回 A−E。已结清交易满足：信号毛价差＋ETF价格影响＋实物股票价格影响＋现金替代价格盈亏＋预估现金影响−投资者税费＝净盈亏。
税费总额不再重复扣减；尚未确定的金额不填零。</p>
<p><a href="trade_attribution.csv">下载逐笔价差与现金盈亏 CSV</a></p>
<label>筛选：<select id="attribution-filter"><option value="all">全部交易</option><option value="loss">仅亏损</option>
<option value="create">仅申购</option><option value="redeem">仅赎回</option></select></label>
<style>#attribution-table thead th{position:sticky;top:0;z-index:1}
#attribution-table tr[data-result="loss"] td:nth-child(4){color:#b42318}
#attribution-table tr[data-result="win"] td:nth-child(4){color:#067647}</style>
<div style="overflow:auto;max-height:650px"><table id="attribution-table"><thead><tr>'''
    document += ''.join(f'<th>{escape(c)}</th>' for c in columns) + '</tr></thead><tbody>'
    for raw, values in zip(rows, shown):
        pnl = decimal(raw.get('net_pnl'))
        status = 'pending' if pnl is None else 'loss' if pnl < 0 else 'win'
        document += f'<tr data-result="{status}" data-side="{escape(raw["side"].lower(), quote=True)}">'
        for column in columns:
            value = escape(str(values[column]))
            if column == '交易编号':
                value = f'<a href="ledger.html#trade-{quote(raw["trade_id"], safe="")}">{value}</a>'
            document += '<td style="white-space:nowrap">' + value + '</td>'
        document += '</tr>'
    return document + '''</tbody></table></div><script>
document.getElementById('attribution-filter').addEventListener('change',function(){
 const value=this.value;
 document.querySelectorAll('#attribution-table tbody tr').forEach(function(row){
 row.hidden=!(value==='all'||row.dataset.result===value||row.dataset.side===value);
 });
});</script>'''
