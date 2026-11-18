"""HTML for signal-minute limit-stock market values, weights and per-stock checks."""

from decimal import Decimal, ROUND_HALF_UP
from html import escape

from etf_arb.report.stock_cost import decimal, timestamp


LIMIT_VALUE_LINK = '<p class="lv-top"><a href="#limit-values">查看信号时涨跌停股票总市值及 ETF 净值占比 ↓</a></p>'

_LABELS = {
    "provided": "已提供的当日限价",
    "provided_no_limit": "明确无价格限制",
    "no_limit": "明确无价格限制",
    "estimated": "前收推算",
    "local_exact_previous_close": "前收推算",
    "verified_historical_previous_close_feed": "已核验前收推算",
    "estimated_open_0930_proxy": "09:30 close 开盘代理",
    "same_day_0930_close_proxy": "09:30 close 开盘代理",
    "missing_previous_close": "缺参考价格",
    "missing_exact_close": "缺信号分钟价格",
    "missing_bounds": "缺有效限价",
    "unknown_board": "板块待核",
    "needs_official_bounds": "需要当日正式限价",
    "opening_proxy_not_yet_available": "开盘代理价尚不可用",
    "conflicting_exact_close": "同分钟价格冲突",
    "invalid_exact_close": "分钟价格无效",
    "missing_pcf": "缺当日申赎清单",
    "missing_signal_iopv": "缺信号 IOPV",
}
_LIMIT_SIDE = {"up": "涨停", "down": "跌停"}


def _label(raw):
    raw = str(raw or "").removeprefix("unknown:")
    if raw.startswith("outside_"):
        return "价格超出限价区间"
    return _LABELS.get(raw, escape(raw) if raw else "—")


def _number(value, places=None):
    number = decimal(value)
    if number is None:
        return "待核"
    if places is None:
        return format(number.normalize(), ",f")
    rounded = number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return format(rounded, f",.{places}f")


def _percent(value):
    ratio = decimal(value)
    return "待核" if ratio is None else _number(ratio * 100, 4) + "%"


def _amount(group):
    if group["known_count"] is None:
        return '<span class="lv-warn">待核</span>'
    if group["complete"]:
        value = group["value"]
        note = f'<small>{group["count"]} 只</small>'
    else:
        value = group["known_value"]
        note = f'<small class="lv-warn">已识别 {group["known_count"]} 只，合计待核</small>'
    return f'<span class="lv-amt">{_number(value, 2)}</span>{note}'


def _local_amount(group):
    if group["known_count"] is None:
        return '<span class="lv-warn">待核</span>'
    if group["complete"]:
        value, weight, note = group["value"], group["weight"], ""
    else:
        value, weight = group["known_value"], group["known_weight"]
        note = ' <span class="lv-warn">· 已识别部分</span>'
    return f'<span class="lv-amt">{_number(value, 2)}</span><small>{_percent(weight)}{note}</small>'


def _stock_table(stocks, *, unknown):
    rows = []
    for stock in stocks:
        symbol = escape(str(stock["symbol"]))
        name = escape(str(stock.get("name") or ""))
        source = _label(stock.get("reference_source") or stock.get("source"))
        if stock.get("reference_price") is not None:
            source += f'<small>参考价 {_number(stock["reference_price"], 2)} 元</small>'
        if unknown:
            status = _label(stock.get("reason"))
            boundary = "—"
            local = "—"
        else:
            direction = stock["limit_side"]
            status = _LIMIT_SIDE[direction]
            boundary = _number(stock.get("upper_limit" if direction == "up" else "lower_limit"), 2)
            local = "计入" if stock["local_directional"] else "—"
        rows.append(f'<tr><td class="lv-left">{name}<small>{symbol}</small></td><td class="lv-left">{status}</td>'
                    f'<td>{_number(stock["quantity"])}</td><td>{_number(stock.get("price"), 2)}</td>'
                    f'<td>{_number(stock.get("value"), 2)}</td><td>{boundary}</td>'
                    f'<td class="lv-left">{source}</td><td class="lv-c">{local}</td></tr>')
    return ('<table><thead><tr><th class="lv-left">股票</th><th class="lv-left">状态</th><th>本笔股数</th>'
            '<th>信号价（元）</th><th>股票市值（元）</th><th>对应限价（元）</th><th class="lv-left">限价依据</th>'
            '<th class="lv-c">本市场方向性</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table>")


def _trade_rows(row, index):
    full, local = row["all"], row["local_directional"]
    total = full["total"]
    trade_id = escape(str(row["trade_id"]))
    side = str(row["side"]).upper()
    direction = {"CREATE": "申购", "REDEEM": "赎回"}.get(side, "待核")
    when = timestamp(row["opened_at"]).strftime("%Y-%m-%d %H:%M")
    basket = _number(row.get("basket_value"), 2)
    coverage = f'{full["identified_count"]}/{full["component_count"]} 只'
    issue = _label(row["coverage_issue"]) if row.get("coverage_issue") else ""
    if issue:
        coverage = f'<span class="lv-warn">{issue}</span>'
    elif full["unknown_count"]:
        coverage += f'<small class="lv-warn">{full["unknown_count"]} 只待核</small>'
    weight = _percent(total["weight"] if total["complete"] else total["known_weight"])
    if not total["complete"] and total["known_count"] is not None:
        weight += '<small class="lv-warn">已识别部分</small>'
    hits = row["hit_stocks"]
    unknowns = row["unknown_stocks"]
    detail_id = f"lv-detail-{index}"
    subset = "yes" if local["known_count"] else "no"
    parts = [f'''<tbody class="lv-trade" data-id="{trade_id}" data-side="{escape(side)}" data-local="{subset}"
 data-hit="{"yes" if hits else "no"}" data-unknown="{"yes" if unknowns or issue else "no"}">
<tr><td class="lv-left"><span class="lv-amt">{when}</span><small>{trade_id}</small></td><td class="lv-c">{direction}</td>
<td>{_amount(full["up"])}</td><td>{_amount(full["down"])}</td><td class="lv-main">{_amount(total)}</td>
<td class="lv-amt">{basket}</td><td class="lv-main">{weight}</td><td>{_local_amount(local)}</td>
<td class="lv-c"><small>{coverage}</small><button type="button" class="lv-inspect" aria-expanded="false"
 aria-controls="{detail_id}">查看明细</button></td></tr><tr class="lv-detail" id="{detail_id}" hidden><td colspan="9">
<h3>{trade_id} · {when} · {direction}</h3><p>本笔 ETF 份额 {_number(row.get("etf_shares"))} 份，
信号 IOPV {_number(row.get("iopv"), 8)} 元/份，篮子参考净值 {basket} 元。占比按原始精度计算。</p>''']
    if issue:
        parts.append(f'<p class="lv-warn">{issue}。</p>')
    else:
        parts.append(f'<p>全篮已判定 {full["identified_count"]}/{full["component_count"]} 只。'
                     f'本市场已判定 {local["identified_count"]}/{local["component_count"]} 只，'
                     f'方向为{_LIMIT_SIDE.get(local["limit_side"], "待核")}。</p>')
    if hits:
        parts.append(_stock_table(hits, unknown=False))
    elif not issue:
        parts.append("<p>已判定股票中没有触及涨跌停的记录。</p>")
    if unknowns:
        parts.extend(('<p><strong>待核股票</strong>　这些股票的涨跌停状态待确认。</p>',
                      _stock_table(unknowns, unknown=True)))
    return "".join(parts) + "</td></tr></tbody>"


def limit_value_html(data):
    """Render one independent appendix per report; weights in the data are ratios."""
    rows = data["rows"]
    title = '<section id="limit-values" aria-labelledby="lv-title"><h2 id="lv-title">信号时涨跌停股票总市值及 ETF 净值占比</h2>'
    if not rows:
        return title + "<p>本轮没有已发起的套利交易。</p></section>"
    suffixes = {row.get("etf_market") or str(row.get("etf_symbol", "")).rpartition(".")[2] for row in rows}
    local_scope = all(row["local_directional"].get("market_only") is True for row in rows)
    market = {"SH": "沪市", "SZ": "深市"}.get(next(iter(suffixes)), "") if local_scope and len(suffixes) == 1 else ""
    complete = sum(bool(row["all"]["total"]["complete"]) for row in rows)
    header = f'''<p>每笔套利按<strong>发起信号当分钟</strong>计一次。全篮涨停市值与跌停市值相加，除以本笔 ETF 份额对应的盘中参考净值，得到涨跌停总市值占比。</p>
<p class="lv-note">{market}方向性敞口按申购看涨停、赎回看跌停。全篮统计包含跨市场现金替代股，方向性敞口排除跨市场现金代理。金额单位为元。</p>
<details class="lv-method"><summary>查看计算公式与限价依据</summary>
<p>个股市值 = 当日 PCF 股数 × 本笔申赎单位数 × 信号分钟收盘价。全篮统计有正股数的成分股，同一只股票每笔只算一次。</p>
<p>本笔 ETF 篮子参考净值 = 信号 IOPV × 本笔 ETF 份额。两种占比使用同一个分母，不同发起时点分别计算。</p>
<p>优先使用当日已提供的有效限价，缺限价时按有效前收和适用涨跌幅推算。前收缺失时使用当日 09:30 分钟 close 作为开盘代理价，逐股明细列出实际采用的依据。</p>
<p>缺少精确行情、限价依据不足或价格超出限价区间时，列出已识别部分与待核股数。零股数固定现金项没有股票市值。本节金额仅衡量信号时的持股规模。</p></details>
<p class="lv-note">全篮完整识别 {complete} 笔，另有 {len(rows) - complete} 笔待核。表内已识别部分可展开核对。</p>
<div class="lv-tools"><label>交易方向 <select id="lv-side"><option value="all">全部方向</option><option value="CREATE">申购</option><option value="REDEEM">赎回</option></select></label>
<label>显示 <select id="lv-filter"><option value="all">全部交易</option><option value="hit">已识别有涨跌停</option><option value="local">有{market}方向性敞口</option>
<option value="unknown">有待核股票</option></select></label><span class="lv-status" id="lv-status" aria-live="polite"></span></div>
<div class="lv-scroll" role="region" tabindex="0" aria-labelledby="lv-title"><table id="lv-values"><thead><tr><th class="lv-left">信号时间 / 交易</th><th class="lv-c">方向</th><th>全篮涨停市值</th><th>全篮跌停市值</th><th>涨跌停总市值</th>
<th>本笔 ETF 篮子净值<small>IOPV × ETF 份额</small></th><th>涨跌停总市值<br>占净值比例</th><th>{market}方向性敞口<small>市值 / 净值占比</small></th><th class="lv-c">识别覆盖 / 明细</th></tr></thead>'''
    footer = '</table></div><p id="lv-empty" hidden>当前条件下没有可显示的交易。</p></section>'
    return ("<style>" + _CSS + "</style>" + title + header
            + "".join(_trade_rows(row, index) for index, row in enumerate(rows, 1)) + footer
            + "<script>" + _JS + "</script>")


_CSS = """
#limit-values { margin: 44px 0 32px; padding-top: 18px; border-top: 2px solid #75879a; scroll-margin-top: 15px; }
#limit-values h2 { margin: 0 0 12px; font-size: 1.45rem; }
#limit-values p { margin: 6px 0; line-height: 1.55; }
#limit-values .lv-note, .lv-top { color: #46515e; font-size: .94rem; }
#limit-values .lv-method { margin: 12px 0 16px; }
#limit-values summary, #limit-values button, #limit-values select { cursor: pointer; }
#limit-values .lv-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 16px; margin: 18px 0 10px; }
#limit-values :is(button,select) { font: inherit; color: #23384e; background: white; border: 1px solid #a3afbc; border-radius: 4px; padding: 4px 8px; }
#limit-values .lv-tools label { white-space: nowrap; }
#limit-values .lv-status { margin-left: auto; color: #46515e; font-size: .9rem; }
#limit-values .lv-scroll { max-height: 70vh; overflow: auto; }
#lv-values > thead th { position: sticky; top: 0; z-index: 1; }
#limit-values table { margin: 0; width: 100%; min-width: 940px; font-size: .91rem; line-height: 1.5; font-variant-numeric: tabular-nums; }
#limit-values th, #limit-values td { padding: 9px 10px; text-align: right; vertical-align: top; }
#limit-values th { background: #f0f3f6; font-weight: 600; }
#limit-values .lv-left { text-align: left; }
#limit-values .lv-c { text-align: center; }
#limit-values small { display: block; font-size: .78rem; color: #535e68; font-weight: normal; }
#limit-values .lv-amt { white-space: nowrap; }
#limit-values .lv-main { background: #f3f7fa; font-weight: 650; }
#limit-values .lv-warn { color: #82500d; }
#limit-values .lv-inspect { margin-top: 2px; font-size: .80rem; padding: 2px 7px; }
#limit-values .lv-detail > td { background: #f8fafb; text-align: left; padding: 12px 16px; }
#limit-values .lv-detail h3 { font-size: 1rem; margin: 4px 0 6px; }
#limit-values .lv-detail table { min-width: 640px; max-width: 1100px; font-size: .86rem; }
#limit-values .lv-detail :is(th,td) { border-bottom-color: #dde3e9; padding: 5px 10px; }
#limit-values [hidden] { display: none !important; }
.lv-top { margin: -4px 0 16px; line-height: 1.6; }
.lv-top a { margin-left: .7rem; }
@media(max-width:760px){ #limit-values .lv-status { width: 100%; margin-left: 0; } }
@media print { #limit-values .lv-tools,#limit-values .lv-inspect,.lv-top a { display: none; } #limit-values tbody.lv-trade[hidden] { display: table-row-group !important; } #limit-values .lv-scroll { max-height: none; overflow: visible; } #lv-values > thead th { position: static; } }
"""

_JS = """
(() => {
  const section = document.getElementById("limit-values");
  const rows = Array.from(section.querySelectorAll("tbody.lv-trade"));
  const side = section.querySelector("#lv-side");
  const state = section.querySelector("#lv-filter");
  const viewport = section.querySelector(".lv-scroll");
  function filterRows() {
    let count = 0;
    for (const row of rows) {
      const matches = (side.value === "all" || row.dataset.side === side.value) &&
        (state.value === "all" || row.dataset[state.value] === "yes");
      row.hidden = !matches;
      if (matches) count++;
    }
    section.querySelector("#lv-status").textContent = count === rows.length
      ? "共 " + count + " 笔" : "显示 " + count + " 笔，共 " + rows.length + " 笔";
    section.querySelector("#lv-empty").hidden = count > 0;
    viewport.scrollTop = 0;
  }
  for (const filter of [side, state]) filter.addEventListener("change", filterRows);
  section.addEventListener("click", event => {
    const button = event.target.closest(".lv-inspect");
    if (!button) return;
    const detail = document.getElementById(button.getAttribute("aria-controls"));
    const open = detail.hidden;
    detail.hidden = !open;
    button.setAttribute("aria-expanded", String(open));
    button.textContent = open ? "收起明细" : "查看明细";
  });
  filterRows();
})();
"""
