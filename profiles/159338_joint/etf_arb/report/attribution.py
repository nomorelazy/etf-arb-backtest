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
REPORT_COLUMNS = {
    'trade_id': '交易编号', 'opened_at': '发起时间', 'side': '方向',
    'net_pnl': '最终净盈亏（元）', 'signal_premium_rate': '信号折溢价率',
    'signal_gross_edge': '信号毛价差（元）',
    'cash_substitution_pnl': '现金替代价格盈亏（元，含代理费）',
    'estimated_cash_pnl': '预估现金盈亏影响（元）',
    'cash_substitution_cash_amount': '现金替代退补／应收款（元，非利润）',
    'estimated_cash': '预估现金E（元）', 'actual_cash': '实际现金A（元）',
    'etf_price_effect': 'ETF价格变化影响（元）',
    'physical_price_effect': '实物股票价格影响（元）',
    'investor_fees': '投资者税费（元）',
    'manager_fees': '代理税费（元，已含在替代盈亏）',
    'cash_substitution_valued_quantity': '未成交估值数量',
}
REPORT_FIELDS = tuple(REPORT_COLUMNS)
DYNAMIC_COLUMNS = dict(REPORT_COLUMNS, estimated_cash='盘前PCF预估现金（元）',
                       estimated_cash_pnl='动态现金估计的盈亏影响（元）',
                       signal_cash_adjustment='信号碎股现金修正（元）',
                       signal_cash_estimate='信号动态现金估计（元）')


def report_columns(rows):
    return DYNAMIC_COLUMNS if any('signal_cash_estimate' in row for row in rows) else REPORT_COLUMNS


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
    fixed = [c.creation_cash_substitute if c.creation_cash_substitute is not None
             else c.substitution_cash_amount for c in mandatory]
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
        etf_symbol = trade.get('etf_symbol') or '159338.SZ'
        etf_fills = [f for f in fill_by_id[tid] if f['symbol'] == etf_symbol and f['purpose'] != 'MANAGER']
        physical_fills = [f for f in fill_by_id[tid] if f['symbol'] != etf_symbol and f['purpose'] != 'MANAGER']
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
        signal_cash = decimal(trade.get('signal_cash_estimate'))
        unit_cash = decimal(signal.get('signal_cash_per_unit'))
        if unit_cash is not None:
            observed = unit_cash * int(trade['quantity_units'])
            if signal_cash is not None and signal_cash != observed:
                raise ValueError(f'Conflicting signal cash estimate: {tid}')
            signal_cash = observed
        if 'signal_cash_estimate' in trade and signal_cash is None:
            raise ValueError(f'Missing stored dynamic cash estimate: {tid}')
        actual = decimal(cash_leg.get('actual_component'))
        cash_final = decimal(cash_leg.get('final'))
        if actual is None and cash_final is not None:
            # Current main's legacy positive-estimate deposit is refunded once.
            actual = max(estimated, ZERO) - cash_final if side == 'CREATE' else cash_final - max(estimated, ZERO)
        signal_basis = estimated if signal_cash is None else signal_cash
        cash_pnl = sign * (actual - signal_basis) if actual is not None else None
        unwound = trade['state'] in {'UNWINDING', 'UNWOUND'}
        pnl = decimal(trade.get('net_pnl')) if trade['state'] in {'CLOSED', 'UNWOUND'} else None
        if unwound:
            etf_effect, physical_effect = None, None
            if pnl is not None:
                def realized_price_change(fill_rows):
                    return sum((decimal(f['price']) * int(f['quantity']) *
                                (1 if f['side'] == 'SELL' else -1) for f in fill_rows), ZERO)
                etf_effect = realized_price_change(etf_fills)
                physical_effect = realized_price_change(physical_fills)
                if abs(pnl - (etf_effect + physical_effect - investor_fees)) > Decimal('0.000001'):
                    raise ValueError(f'Unwind cash flows do not reconcile: {tid}')
        components = (edge, etf_effect, physical_effect, sub_pnl, cash_pnl)
        residual = None
        if not unwound and pnl is not None and all(value is not None for value in components):
            residual = pnl - (sum(components, ZERO) - investor_fees)
            if abs(residual) > Decimal('0.000001'):
                raise ValueError(f'Trade report attribution does not reconcile: {tid}: {residual}')
        result.append(dict(trade_id=tid, opened_at=trade['opened_at'], applied_at=trade.get('applied_at'),
            settled_at=trade.get('settled_at'), side=side, state=trade['state'], etf_shares=shares,
            signal_etf_price=etf_price, signal_iopv=iopv, signal_premium_rate=premium,
            signal_spread_per_share=etf_price-iopv if etf_price is not None and iopv is not None else None,
            signal_gross_edge=None if unwound else edge, net_pnl=pnl, etf_price_effect=etf_effect,
            physical_price_effect=physical_effect, cash_substitution_signal_basis=sub_reference,
            cash_substitution_actual_value=sub_actual, cash_substitution_pnl=sub_pnl,
            cash_substitution_cash_amount=sub_final, cash_substitution_cash_estimate=decimal(sub_leg.get('estimate')),
            cash_substitution_deposit=deposit, cash_substitution_status=leg_status(sub_leg),
            cash_substitution_priced_at=sub_leg.get('priced_at'), cash_substitution_paid_at=sub_leg.get('paid_at'),
            cash_substitution_valued_quantity=sum(int(r['quantity']) for r in pricing_by_id[tid]),
            estimated_cash=None if unwound else estimated, actual_cash=actual, estimated_cash_pnl=cash_pnl,
            cash_difference_cash_amount=cash_final, cash_difference_status=leg_status(cash_leg),
            cash_difference_known_on=cash_leg.get('known_on'), cash_difference_paid_at=cash_leg.get('paid_at'),
            investor_fees=investor_fees, manager_fees=manager_fees, total_fees=total_fees,
            attribution_residual=residual,
            attribution_status=('未提交申赎，已卖出' if pnl is not None else '未提交申赎，待卖出') if unwound
            else '已核对' if residual is not None else '待结清' if pnl is None else '缺信号基准'))
        if signal_cash is not None:
            result[-1].update(signal_cash_estimate=signal_cash, signal_cash_adjustment=signal_cash-estimated)
    return result


def save_attribution_csv(path, rows, *, dynamic=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=DYNAMIC_COLUMNS if dynamic else report_columns(rows), extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    return path


def display_row(row, columns=None):
    def cell(name):
        if name in ('trade_id', 'opened_at', 'cash_substitution_valued_quantity'):
            return row[name]
        if name == 'side':
            return '申购' if row[name] == 'CREATE' else '赎回'
        value = decimal(row.get(name))
        if value is None:
            return '未确定'
        return f'{value:+.4%}' if name == 'signal_premium_rate' else f'{value:,.2f}'
    return {label: cell(name) for name, label in (columns or report_columns([row])).items()}


def attribution_totals(rows, columns=None):
    """Sum exact source amounts; missing prices never become zero profit."""
    result = {'trade_id': '合计', 'opened_at': f'共 {len(rows)} 笔',
              'side': '—', 'signal_premium_rate': '—'}
    columns = columns or report_columns(rows)
    for field in columns:
        if field in result:
            continue
        values = [decimal(row.get(field)) for row in rows]
        known = [value for value in values if value is not None]
        missing = len(values) - len(known)
        if missing and not known:
            result[field] = f'未确定（{missing} 笔）'
            continue
        total = sum(known, ZERO)
        result[field] = f'{total:,.0f}' if field == 'cash_substitution_valued_quantity' else f'{total:,.2f}'
        if missing:
            result[field] += f'（已知合计；{missing} 笔未确定）'
    return {label: result[field] for field, label in columns.items()}


def attribution_html(rows):
    groups = {
        'all': rows,
        'loss': [row for row in rows if decimal(row.get('net_pnl')) is not None and decimal(row['net_pnl']) < 0],
        'create': [row for row in rows if row['side'] == 'CREATE'],
        'redeem': [row for row in rows if row['side'] == 'REDEEM'],
    }
    selected_columns = report_columns(rows)
    totals = {name: attribution_totals(selected, selected_columns) for name, selected in groups.items()}
    creation_count = len(groups['create'])
    redemption_count = len(groups['redeem'])
    counts = (f'<p id="trade-counts"><strong>申购 {creation_count} 笔，赎回 {redemption_count} 笔，'
              f'合计 {len(rows)} 笔。</strong>按本轮实际发起的套利统计。</p>')
    net_column = REPORT_COLUMNS['net_pnl']
    direction_pnl = (
        '<p id="direction-pnl-summary"><strong>申购总盈亏（元）：</strong>'
        f'<span id="creation-net-pnl">{escape(totals["create"][net_column])}</span>；'
        '<strong>赎回总盈亏（元）：</strong>'
        f'<span id="redemption-net-pnl">{escape(totals["redeem"][net_column])}</span>。</p>'
        '<p>按本轮全部交易的最终净盈亏分别汇总，不随下方筛选变化；'
        '存在未结清交易时，仅显示已结清合计并注明未确定笔数。</p>')
    if not rows:
        return '<h2>逐笔价差与现金盈亏</h2>' + counts + direction_pnl + '<p>本轮没有已发起交易。</p>'
    shown = [display_row(r, selected_columns) for r in rows]
    columns = list(shown[0])
    cash_note = ('本表 E 是发起时的动态现金估计，A 是正式现金差额，均按本笔申赎单位数计。'
                 if selected_columns is DYNAMIC_COLUMNS else '')
    document = '<h2 id="trade-attribution">逐笔价差与现金盈亏</h2>' + counts + direction_pnl + cash_note + '''
<p>现金替代价格盈亏＝相对信号估值的价格变化，已包含代理税费；退补款／应收款是资金收付，不等于利润。
预估现金影响：申购 E−A、赎回 A−E。已结清交易满足：信号毛价差＋ETF价格影响＋实物股票价格影响＋现金替代价格盈亏＋预估现金影响−投资者税费＝净盈亏。
税费总额不再重复扣减；尚未确定的金额不填零。</p>
<p>表尾合计随筛选更新，金额按未四舍五入的原值相加后显示到分，未成交估值数量按股数相加。
编号、时间、方向和折溢价率不求和；存在未确定金额时，仅列已知合计并注明缺失笔数。</p>
<p><a href="trade_attribution.csv">下载逐笔价差与现金盈亏 CSV</a></p>
<label>筛选：<select id="attribution-filter"><option value="all">全部交易</option><option value="loss">仅亏损</option>
<option value="create">仅申购</option><option value="redeem">仅赎回</option></select></label>
<style>#attribution-table{width:100%;min-width:0;table-layout:fixed;font-size:clamp(9px,.78vw,13px)}
#attribution-table th,#attribution-table td{padding:8px 4px;white-space:normal;overflow-wrap:anywhere;vertical-align:top;line-height:1.5;text-align:right}
#attribution-table th{font-weight:600}#attribution-table th:nth-child(-n+3),#attribution-table td:nth-child(-n+3){text-align:left}
#attribution-table tfoot td{font-weight:700;background:#eaf1f8;border-top:2px solid #6b8299}
#attribution-table tr[hidden]{display:none}
#attribution-table tr[data-result="loss"] td:nth-child(4){color:#b42318}
#attribution-table tr[data-result="win"] td:nth-child(4){color:#067647}</style>
<table id="attribution-table"><thead><tr>'''
    document += ''.join(f'<th>{escape(c)}</th>' for c in columns) + '</tr></thead><tbody>'
    for raw, values in zip(rows, shown):
        pnl = decimal(raw.get('net_pnl'))
        status = 'pending' if pnl is None else 'loss' if pnl < 0 else 'win'
        document += f'<tr data-result="{status}" data-side="{escape(raw["side"].lower(), quote=True)}">'
        for column in columns:
            value = escape(str(values[column]))
            if column == '交易编号':
                value = f'<a href="ledger.html#trade-{quote(raw["trade_id"], safe="")}">{value}</a>'
                if raw.get('state') in {'UNWINDING', 'UNWOUND'}:
                    value += '（未申赎）'
            document += '<td>' + value + '</td>'
        document += '</tr>'
    document += '</tbody><tfoot>'
    for name, group_totals in totals.items():
        hidden = '' if name == 'all' else ' hidden'
        document += f'<tr data-summary-filter="{name}"{hidden}>'
        document += ''.join(f'<td>{escape(str(group_totals[column]))}</td>' for column in columns) + '</tr>'
    return document + '''</tfoot></table><script>
document.getElementById('attribution-filter').addEventListener('change',function(){
 const value=this.value;
 document.querySelectorAll('#attribution-table tbody tr').forEach(function(row){
 row.hidden=!(value==='all'||row.dataset.result===value||row.dataset.side===value);
 });
 document.querySelectorAll('#attribution-table tfoot tr').forEach(function(row){
 row.hidden=row.dataset.summaryFilter!==value;
 });
});</script>'''
