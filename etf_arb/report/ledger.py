"""Standard report companion containing every account event for each cycle."""

from collections import defaultdict
from decimal import Decimal
from html import escape
from pathlib import Path


def _table(rows):
    rows = list(rows)
    if not rows:
        return "<p>无记录</p>"
    columns = list(rows[0])
    def cell(value):
        return escape("" if value is None else str(value))
    return ("<div class='scroll'><table><thead><tr>"
            + "".join(f"<th>{cell(key)}</th>" for key in columns)
            + "</tr></thead><tbody>"
            + "".join("<tr>" + "".join(f"<td>{cell(row.get(key))}</td>" for key in columns) + "</tr>" for row in rows)
            + "</tbody></table></div>")


def save_ledger_report(path, trades, cashflows, fills, settlements, pricing_rows=(), attempt_rows=(), creation_substitutions=()):
    groups = []
    for records in (cashflows, fills, settlements, pricing_rows, attempt_rows, creation_substitutions):
        grouped = defaultdict(list)
        for row in records:
            grouped[row['trade_id']].append(row)
        groups.append(grouped)
    document = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<title>逐笔完整账本</title><style>
body{font-family:system-ui,sans-serif;margin:28px;color:#20252b;line-height:1.6}
table{border-collapse:collapse;font-size:13px;white-space:nowrap;margin:14px 0}
th,td{border:1px solid #ddd;padding:7px 10px;text-align:right}th{background:#f4f6f8}
.scroll{overflow:auto}details{border:1px solid #ccc;margin:18px 0;padding:15px}summary{cursor:pointer}
@media print{button{display:none}.scroll{overflow:visible}table{white-space:normal;font-size:9px}details{break-before:page}}
</style><h1>逐笔完整账本</h1>
<p>现金流入为正、流出为负。买入支出不等于亏损，卖出收入不等于盈利；全部交易腿及款项结清后的现金流合计才是最终盈亏。</p>
<p>基金经理代买卖是现金替代计算依据，不重复计入投资者现金流水；定价日收盘估值不伪装成成交。当前使用 close 简化成交，手续费及额外滑点为零。</p>
<button onclick="document.querySelectorAll('details').forEach(d=>d.open=true)">展开全部</button>
<button onclick="document.querySelectorAll('details').forEach(d=>d.open=true);window.print()">打印全部</button>"""
    for trade in trades:
        tid = trade['trade_id']
        flows = groups[0][tid]
        total = sum((Decimal(str(row['cash_delta'])) for row in flows), Decimal('0'))
        closed = trade['state'] == 'CLOSED'
        if closed and total != Decimal(str(trade['net_pnl'])):
            raise ValueError(f"Trade cash-flow reconciliation failed: {tid}")
        label = "最终盈亏" if closed else "累计现金变动（未结清）"
        document += f"<details><summary>{escape(tid)} | {escape(str(trade['opened_at']))} | {label} {total:+f} 元</summary>"
        document += _table([trade]) + "<h2>全部账户现金及证券事件</h2>" + _table(flows)
        document += "<h2>投资者成交</h2>" + _table(row for row in groups[1][tid] if row['purpose'] != 'MANAGER')
        document += "<h2>深市成分股买入转现金替代（保证金计算）</h2>" + _table(groups[5][tid])
        document += "<h2>现金替代代买卖尝试（含未成交原因）</h2>" + _table(groups[4][tid])
        document += "<h2>现金替代代买卖计算依据（不重复加减现金）</h2>" + _table(row for row in groups[1][tid] if row['purpose'] == 'MANAGER')
        document += "<h2>未成交部分在定价期限日的估值</h2>" + _table(groups[3][tid])
        document += "<h2>定价、通知与付款状态</h2>" + _table(groups[2][tid]) + "</details>"
    document += "</html>"
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document, encoding='utf-8')
    return target
