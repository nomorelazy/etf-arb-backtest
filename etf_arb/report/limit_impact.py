"""Read-only, trade-scoped stock limit attribution from execution records.

Only limit touches at trade opening select stocks. Subsequent fills and deadline
valuations determine their profit without adding later-touching stocks.
"""

from collections import defaultdict
import csv
from datetime import datetime, time, timedelta
from decimal import Decimal
from html import escape
import json
from pathlib import Path
from urllib.parse import quote

import pyarrow.dataset as ds

from etf_arb.data.minute_close import MinuteCloseBar, MinuteCloseConflictError
from etf_arb.data.pcf import FilePCFRepository
from etf_arb.data.snapshot import component_symbol
from etf_arb.domain import Side
from etf_arb.execution.plan import build_plan
from etf_arb.pricing.iopv import fixed_substitution_amount
from etf_arb.report.stock_cost import decimal, timestamp
from etf_arb.rules import InMemoryRuleRepository
from etf_arb.report.price_limit_bounds import ReportPriceLimits
from etf_arb.runner_config import load_rules

from etf_arb.report.stock_cost import COST_FIELDS, with_cost_metrics

ZERO = Decimal(0)
STAGES = {"signal": "买入／发起套利时"}
ROUTES = {"manager": "现金代买卖", "physical": "实物交割", "fixed": "固定现金替代",
          "unapplied": "未提交申赎", "unwound": "未申赎，已平仓"}
METHODS = {"manager_scheduled_close_proxy": "本市场定时补券成交",
           "manager_next_minute_close_proxy": "跨市场首个可成交分钟",
           "manager_first_tradable_close_proxy": "跨市场首个可成交分钟",
           "unfilled_quantity_at_deadline_close": "未成交部分按期限收盘估值",
           "signal_minute_close": "信号分钟实物买入", "next_minute_close": "实物成交",
           "fixed_pcf_amount": "PCF固定金额", "unwind": "申赎未提交后的实际平仓"}
DETAIL_FIELDS = (
    "trade_id", "opened_at", "side", "symbol", "security_name", "market", "quantity",
    "pcf_quantity", "signal_price", "signal_price_at", "basket_value", "etf_weight",
    "first_limit_at", "limit_types", "limit_stages", "limit_sources", "route",
    "stock_pnl", "cash_substitution_pnl", "physical_pnl", "manager_fees", "investor_stock_fees",
    "filled_quantity", "valued_quantity", "actual_value", "pnl_reason",
    "pricing_method", "execution_price", "valuation_price", "first_execution_at", "last_execution_at",
    "valuation_at", "pricing_deadline", "priced_at", "paid_at",
)
DETAIL_FIELDS += COST_FIELDS
COVERAGE_FIELDS = ("trade_id", "opened_at", "side", "limit_stock_count", "limit_symbols",
                   "unknown_stock_count", "unknown_details", "observation_count")
EVENT_FIELDS = ("trade_id", "symbol", "ts", "stage", "limit_type", "limit_source", "price")


class EventPrices:
    """Batch only requested timestamps by month; no catalog or backward scan."""

    def __init__(self, root):
        self.root = Path(root)
        self.values = {}

    def read(self, requests):
        months = defaultdict(lambda: defaultdict(set))
        for at, symbols in requests.items():
            months[at.strftime("%Y-%m")][at].update(symbols)
        for month, times in sorted(months.items()):
            files = sorted((self.root / month).glob("*.parquet"))
            if not files:
                continue
            symbols = set().union(*times.values())
            table = ds.dataset([str(p) for p in files], format="parquet").to_table(
                columns=["time", "thscode", "close"],
                filter=ds.field("time").isin(sorted(times)) & ds.field("thscode").isin(sorted(symbols)))
            for at, symbol, raw in zip(*(table[n].to_pylist() for n in ("time", "thscode", "close"))):
                if symbol not in times.get(at, ()) or raw is None:
                    continue
                price = decimal(raw)
                if price <= 0:
                    raise ValueError(f"Invalid limit report price: {symbol} {at}")
                key = symbol, at
                if key in self.values and self.values[key] != price:
                    raise MinuteCloseConflictError(f"Conflicting limit report price: {symbol} {at}")
                self.values[key] = price

    def load(self, day, symbols, *, at=None):
        # ReportPriceLimits's previous-close adapter. All prices are preloaded.
        pass

    def get(self, symbol, now, *, exact=False):
        price = self.values.get((symbol, now))
        if price is not None:
            return MinuteCloseBar(now, symbol, price)
        return None

    def signal(self, symbol, now):
        # Match the existing report signal reference's t / t-1 convention only.
        return self.get(symbol, now) or self.get(symbol, now - timedelta(minutes=1))


def _group(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["trade_id"], row["symbol"]].append(row)
    return grouped


def _stock_pnl(trade, component, plan, symbol, signal_price, fills, fees, pricing):
    """Decompose existing flows. Deposits/refunds are never stock profits."""
    side, units = trade["side"], int(trade["quantity_units"])
    sign = -1 if side == "CREATE" else 1
    route = "manager" if symbol in plan.manager else "physical" if symbol in plan.physical else "fixed"
    result = dict(route=route, stock_pnl=None, cash_substitution_pnl=None, physical_pnl=None,
                  manager_fees=ZERO, investor_stock_fees=ZERO, filled_quantity=0,
                  valued_quantity=0, actual_value=None, pnl_reason="missing_signal_price",
                  pricing_method="", execution_price=None, valuation_price=None,
                  first_execution_at=None, last_execution_at=None, valuation_at=None)
    applied = trade.get("applied_at") not in (None, "")
    if not applied:
        result.update(route="unapplied", pnl_reason="application_not_submitted")
        buys = [r for r in fills if r["purpose"] == "ENTRY" and r["side"] == "BUY"]
        sells = [r for r in fills if r["purpose"] == "UNWIND" and r["side"] == "SELL"]
        bought, sold = sum(int(r["quantity"]) for r in buys), sum(int(r["quantity"]) for r in sells)
        if bought and bought == sold:
            costs = sum((decimal(r["total"]) for r in fees if r["purpose"] in {"ENTRY", "UNWIND"}), ZERO)
            value = sum((decimal(r["price"]) * int(r["quantity"]) for r in sells), ZERO)
            pnl = value - sum((decimal(r["price"]) * int(r["quantity"]) for r in buys), ZERO) - costs
            result.update(route="unwound", stock_pnl=pnl, physical_pnl=pnl, investor_stock_fees=costs,
                          filled_quantity=sold, execution_price=value / sold, actual_value=value,
                          pricing_method="unwind", first_execution_at=min(timestamp(r["ts"]) for r in buys),
                          last_execution_at=max(timestamp(r["ts"]) for r in sells), pnl_reason="unwound")
        return result
    if route == "fixed":
        basis = fixed_substitution_amount(component) * units
        actual = fixed_substitution_amount(component, redemption=side == "REDEEM") * units
        result.update(stock_pnl=sign * (actual - basis), cash_substitution_pnl=sign * (actual - basis),
                      actual_value=actual, pnl_reason="fixed_pcf_amount", pricing_method="fixed_pcf_amount")
        return result
    purpose = "MANAGER" if route == "manager" else "ENTRY" if side == "CREATE" else "EXIT"
    expected_side = "BUY" if side == "CREATE" else "SELL"
    selected = [r for r in fills if r["purpose"] == purpose and r["side"] == expected_side]
    selected_fees = [r for r in fees if r["purpose"] == purpose and r["side"] == expected_side]
    total_fees = sum((decimal(r["total"]) for r in selected_fees), ZERO)
    filled = sum(int(r["quantity"]) for r in selected)
    values = pricing if route == "manager" else []
    valued = sum(int(r["quantity"]) for r in values)
    required = plan.manager[symbol] if route == "manager" else plan.physical[symbol]
    if filled + valued > required:
        raise ValueError(f"Duplicated stock pricing: {trade['trade_id']} {symbol}")
    result.update(filled_quantity=filled, valued_quantity=valued)
    result["manager_fees" if route == "manager" else "investor_stock_fees"] = total_fees
    fill_value = sum((decimal(r["price"]) * int(r["quantity"]) for r in selected), ZERO)
    priced_value = sum((decimal(r["price"]) * int(r["quantity"]) for r in values), ZERO)
    methods = {r.get("basis") or "actual_fill" for r in selected}
    if valued:
        methods.add("unfilled_quantity_at_deadline_close")
    result.update(pricing_method=";".join(sorted(methods)),
        execution_price=fill_value / filled if filled else None,
        valuation_price=priced_value / valued if valued else None,
        first_execution_at=min((timestamp(r["ts"]) for r in selected), default=None),
        last_execution_at=max((timestamp(r["ts"]) for r in selected), default=None),
        valuation_at=max((timestamp(r["ts"]) for r in values), default=None))
    if filled + valued != required:
        result["pnl_reason"] = "stock_not_fully_priced"
        return result
    value = fill_value + priced_value
    result["actual_value"] = value + (-sign * total_fees) if route == "manager" else value
    if signal_price is not None:
        pnl = sign * (value - required * signal_price) - total_fees
        result["stock_pnl"] = pnl
        result["cash_substitution_pnl" if route == "manager" else "physical_pnl"] = pnl
        result["pnl_reason"] = "filled_and_valued" if valued else "filled"
    return result


def build_limit_impact(config, trades, signals, fills, fees, pricing_rows=(), substitutions=(),
                       settlements=(), attempts=(), *, rules=None, application_blocks=()):
    """Build only after execution, for both fresh runs and saved-run rebuilds."""
    def setting(name, default=None):
        return config.get(name, default) if isinstance(config, dict) else getattr(config, name, default)

    data = dict(schema_version=3, selection_basis="trade_open", rows=[], coverage=[], events=[])
    if not trades:
        return data
    if rules is None:
        rules = load_rules(setting("rules_path")) if setting("rules_path") else InMemoryRuleRepository()
    pcfs = FilePCFRepository(setting("pcf_root"))
    cache = {}

    def pcf_for(code, day):
        key = code, day
        if key not in cache:
            cache[key] = pcfs.get(code, day)
        return cache[key]

    fill_map, fee_map, pricing_map = _group(fills), _group(fees), _group(pricing_rows)
    selected = defaultdict(dict)
    for row in substitutions:
        selected[row["trade_id"]][row["symbol"]] = decimal(row["deposit"])
    legs = {r["trade_id"]: r for r in settlements if r["kind"] == "cash_substitution"}
    signal_map = {timestamp(r["ts"]): r for r in signals}
    trade_map = {t["trade_id"]: t for t in trades}
    contexts, events = {}, set()
    requests = defaultdict(set)
    for trade in trades:
        tid, at, units = trade["trade_id"], timestamp(trade["opened_at"]), int(trade["quantity_units"])
        code = (trade.get("etf_symbol") or setting("etf_codes")[0]).split(".")[0]
        pcf = pcf_for(code, at.date())
        plan = build_plan(pcf, Side(trade["side"]), units,
            creation_cash_substitutions={s: value / units for s, value in selected[tid].items()})
        components = {component_symbol(c): c for c in pcf.components
                      if not c.is_cash_placeholder and c.quantity > 0}
        contexts[tid] = code, pcf, plan, components
        for symbol in components:
            events.add((at, tid, symbol, "signal"))
            requests[at - timedelta(minutes=1)].add(symbol)
    # Freeze the cohort at opened_at for both creation and redemption. Later
    # application, proxy attempts, fills and rejections affect PnL, not membership.
    # Keep attempts/application_blocks in the API for saved-run compatibility.
    events = sorted(events)
    for at, tid, symbol, stage in events:
        code = contexts[tid][0]
        event_pcf = pcf_for(code, at.date())
        requests[at].add(symbol)
        requests[datetime.combine(at.date(), time(9, 30))].add(symbol)
        if event_pcf.pre_trading_day and event_pcf.pre_trading_day < at.date():
            requests[datetime.combine(event_pcf.pre_trading_day, time(15))].add(symbol)
    prices = EventPrices(setting("minute_root"))
    prices.read(requests)
    limit_filter = ReportPriceLimits.from_config(prices, config, event_prices=prices)
    data["prior_closes_sha256"] = limit_filter.previous.sha256
    active = None
    hits, unknown, counts = defaultdict(list), defaultdict(set), defaultdict(int)
    # One bounds calculation per opening date/ETF, using that date's PCF.
    day_components = defaultdict(dict)
    for at, tid, symbol, stage in events:
        day_components[contexts[tid][0], at.date()][symbol] = contexts[tid][3][symbol]
    for at, tid, symbol, stage in events:
        code = contexts[tid][0]
        key = code, at.date()
        if active != key:
            limit_filter.on_day_open(pcf_for(*key), rules, check_basket=False,
                                     held_components=day_components[key].values())
            active = key
        status = limit_filter.status(symbol, at)
        counts[tid] += 1
        if status and status.startswith("unknown:"):
            unknown[tid].add((symbol, status))
        elif status:
            kind, source = status.split(":", 1)
            event = dict(trade_id=tid, symbol=symbol, ts=at, stage=stage,
                         limit_type=kind, limit_source=source, price=prices.get(symbol, at).close)
            hits[tid, symbol].append(event)
            data["events"].append(event)

    for (tid, symbol), stock_events in sorted(hits.items(), key=lambda item: (timestamp(trade_map[item[0][0]]["opened_at"]), item[0])):
        trade = trade_map[tid]
        code, pcf, plan, components = contexts[tid]
        at, units = timestamp(trade["opened_at"]), int(trade["quantity_units"])
        component = components[symbol]
        bar = prices.signal(symbol, at)
        signal_price = bar.close if bar else None
        iopv = decimal(signal_map.get(at, {}).get("iopv"))
        basket = iopv * int(trade["etf_shares"]) if iopv is not None and iopv > 0 else None
        quantity = component.quantity * units
        sub_leg = legs.get(tid, {})
        row = dict(trade_id=tid, opened_at=at, side=trade["side"], symbol=symbol,
            security_name=component.security_name, market=symbol.rsplit(".", 1)[-1],
            pcf_quantity=component.quantity, quantity=quantity, etf_shares=int(trade["etf_shares"]), signal_price=signal_price,
            signal_price_at=bar.time if bar else None, basket_value=basket,
            etf_weight=quantity * signal_price / basket if basket and signal_price is not None else None,
            first_limit_at=min(e["ts"] for e in stock_events),
            limit_types=";".join(sorted({e["limit_type"] for e in stock_events})),
            limit_stages=";".join(sorted({e["stage"] for e in stock_events})),
            limit_sources=";".join(sorted({e["limit_source"] for e in stock_events})),
            pricing_deadline=sub_leg.get("pricing_deadline"), priced_at=sub_leg.get("priced_at"),
            paid_at=sub_leg.get("paid_at"))
        row.update(_stock_pnl(trade, component, plan, symbol, signal_price,
            fill_map[tid, symbol], fee_map[tid, symbol], pricing_map[tid, symbol]))
        data["rows"].append(row)
    for trade in trades:
        tid = trade["trade_id"]
        symbols = sorted(symbol for trade_id, symbol in hits if trade_id == tid)
        data["coverage"].append(dict(trade_id=tid, opened_at=trade["opened_at"], side=trade["side"],
            limit_stock_count=len(symbols), limit_symbols=";".join(symbols),
            unknown_stock_count=len({s for s, _ in unknown[tid]}),
            unknown_details=json.dumps(sorted(unknown[tid]), ensure_ascii=False), observation_count=counts[tid]))
    return with_cost_metrics(data, trades)


def save_limit_impact(output, data):
    data = with_cost_metrics(data)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    for name, key, fields in (("stock_limit_impact.csv", "rows", DETAIL_FIELDS),
                              ("trade_limit_summary.csv", "coverage", COVERAGE_FIELDS),
                              ("stock_limit_events.csv", "events", EVENT_FIELDS)):
        with (output / name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(data[key])
    (output / "stock_limit_impact.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def limit_impact_html(data, *, id_suffix=""):
    """Render the report-opening direction and price-only view of the saved all-side attribution."""
    direction = {"CREATE": "limit_up", "REDEEM": "limit_down"}
    selected = [row for row in data["rows"]
                if row["limit_types"] == direction.get(row["side"])
                and row["limit_stages"] == "signal"
                and (not row.get("first_limit_at")
                     or timestamp(row["first_limit_at"]) == timestamp(row["opened_at"]))]
    all_rows = with_cost_metrics({"rows": selected}, price_only=True)["rows"]
    symbols = defaultdict(set)
    for row in selected:
        symbols[row["trade_id"]].add(row["symbol"])
    coverage = [dict(row, limit_stock_count=len(symbols[row["trade_id"]]),
                     limit_symbols=";".join(sorted(symbols[row["trade_id"]])))
                for row in data["coverage"]]
    hidden = [r for r in all_rows if r["fee_only"]]
    rows = [r for r in all_rows if not r["fee_only"]]
    visibility_note = (f"<p>明细已隐藏 {len(hidden)} 条价格影响为零、原盈亏仅来自税费的股票；当前展示 {len(rows)} 条。"
                         "原成交、税费、整笔总盈亏及全部明细 CSV 均保留。</p>"
                         "<p>ETF 整篮成本影响＝该股纯价格影响的相反数，不含交易税费；正数表示申购价格成本增加，"
                         "赎回时表示卖出回收减少（等价成本增加）。每份影响除以本笔 ETF 份数；"
                         "成本影响比例除以信号 IOPV×本笔 ETF 份数，仅为信号整篮估值的比例。"
                         "不代表 ETF 市场价格被实际推动，也不再次计入总盈亏。缺失金额保留未确定。</p>")
    suffix = "-" + quote(id_suffix, safe="") if id_suffix else ""
    table_id = "stock-limit-table" + suffix
    def number(value, places=2):
        return "未确定" if value in (None, "") else f"{decimal(value):,.{places}f}"
    def cell(value):
        return escape(str(value))
    def total(field, route=None):
        applicable = [r for r in rows if route is None or r["route"] in route]
        known = [decimal(r[field]) for r in applicable if r.get(field) not in (None, "")]
        missing = len(applicable) - len(known)
        value = number(sum(known, ZERO)) if known or not applicable else "未确定"
        return value + (f"（{missing} 项未确定）" if missing else "")

    touched = sum(int(r["limit_stock_count"]) > 0 for r in coverage)
    unknown = sum(int(r["unknown_stock_count"]) > 0 for r in coverage)
    doc = f'''<section id="stock-limit-impact{suffix}"><h2>申购涨停／赎回跌停股票价格影响（不含税费）</h2>
<p>检查 {len(coverage)} 笔交易的入场分钟；{touched} 笔涉及申购涨停或赎回跌停，合计 {len(all_rows)} 个“交易×股票”；
{unknown} 笔存在无法判断的股票（详见逐笔检查表）。</p>
<p class="note">仅按发起套利的 t 分钟筛选：申购只保留当时涨停的成分股，赎回只保留当时跌停的成分股。
申购中的跌停、赎回中的涨停不纳入本表。已选股票若在 t+1 恢复成交，仍保留其实际成交及盈亏，不额外假定延迟。
入场时正常、后来申赎提交、补券或卖出时才涨跌停的股票不纳入；后续成交和估值只用于计算已选股票的盈亏。
涨跌停指分钟 close 触及限价，不代表封单或一定无法成交；前收盘估算使用前交易日收盘价和板块规则。缺少原可算限价时，使用当日 09:30 精确 close 作开盘代理估算，注明开盘代理价；这不是真实开盘价或交易所限价，缺少该分钟价格仍为未确定。除权等特殊情形仍需正式限价数据。
ETF 估算权重＝该笔 PCF 股数×信号价÷（信号 IOPV×ETF 份数），不是指数官方权重。</p>
<p class="note">个股价格影响以信号时该股估值为基准：申购为基准减买入或期限估值金额，赎回为卖出或期限估值金额减基准，不含交易税费。
成交和期限估值按本轮已保存的实际处理时点分别列示。
以下金额直接来自本轮已保存成交和估值，历史回测仍按当时执行规则；不以新规则追溯改写。
部分成交的成交均价与剩余估值分别展示，未全部定价的盈亏留空。固定现金替代沿用本轮信号估值与 PCF 实际固定金额；
未提交申赎后平仓的股票按实际买卖价差计算，不含费用。
这些是原回测盈亏的组成，不再次加到总利润，不把保证金退款当盈利，也不把整笔预估现金差额分摊给某只股票。
入选股票的价格变化不等于涨跌停直接造成的损失。定价不等于到账。</p>
{visibility_note}
<p><a href="stock_limit_impact.csv">下载全方向股票明细 CSV（含原始含费字段，纯价格影响见 gross_price_pnl）</a> · <a href="stock_limit_events.csv">下载全方向触及事件 CSV</a> ·
<a href="trade_limit_summary.csv">下载全方向逐笔检查表</a></p>
<style>#{table_id}{{width:100%;min-width:0;table-layout:fixed;font-size:12px}}
#{table_id} th,#{table_id} td{{padding:8px 5px;white-space:normal;overflow-wrap:anywhere;vertical-align:top}}
#{table_id} tfoot{{font-weight:bold;background:#eaf1f8}}</style>
<table id="{table_id}"><thead><tr>'''
    columns = ("交易／发起时间", "方向", "股票／市场", "触及情况", "股数", "ETF 估算权重",
               "信号时股价", "处理方式", "采用的定价方式", "实际成交均价", "未成交部分估值价", "个股价格变动盈亏（元）", "ETF整篮价格成本影响（元）", "每份ETF成本影响（元/份，不含税费）", "占信号整篮估值比例")
    doc += "".join(f"<th>{c}</th>" for c in columns) + "</tr></thead><tbody>"
    for r in rows:
        kinds = r["limit_types"].replace("limit_up", "涨停").replace("limit_down", "跌停")
        stages = "／".join(STAGES.get(s, s) for s in r["limit_stages"].split(";"))
        source = "；".join({"estimated": "前收盘估算",
            "estimated_open_0930_proxy": "09:30 close 开盘代理估算",
            "provided": "提供的限价"}.get(value, value) for value in r["limit_sources"].split(";"))
        weight = "未确定" if r["etf_weight"] in (None, "") else f"{decimal(r['etf_weight']):.4%}"
        method = "／".join(METHODS.get(m, m) for m in r["pricing_method"].split(";") if m) or "未定价"
        if r.get("last_execution_at"):
            method += " " + str(r["last_execution_at"])
        if r.get("valuation_at"):
            method += "；估值 " + str(r["valuation_at"])
        executed = (number(r["execution_price"], 4) + f"（{r['filled_quantity']} 股）") if int(r["filled_quantity"]) else "—"
        valued = (number(r["valuation_price"], 4) + f"（{r['valued_quantity']} 股）") if int(r["valued_quantity"]) else "—"
        values = [f"{r['trade_id']}\n{r['opened_at']}", "申购" if r["side"] == "CREATE" else "赎回",
            f"{r['symbol']}\n{r['security_name'] or ''}", f"{kinds}／{stages}／{source}",
            number(r["quantity"], 0), weight, number(r["signal_price"], 4), ROUTES[r["route"]], method, executed, valued,
            "未参与交割" if r["route"] == "unapplied" else number(r["gross_price_pnl"]), number(r["etf_cost_impact"]),
            number(r["etf_cost_impact_per_share"], 8),
            "未确定" if r["etf_cost_impact_ratio"] is None else f"{decimal(r['etf_cost_impact_ratio']):.6%}"]
        doc += "<tr>" + "".join(f"<td>{cell(v)}</td>" for v in values) + "</tr>"
    if not rows and all_rows:
        doc += '<tr><td colspan="15">本表入选股票的已确定盈亏均仅来自税费，已隐藏；费用仍计入总盈亏。</td></tr>'
    elif not rows:
        doc += '<tr><td colspan="15">未识别到入场时的申购涨停／赎回跌停股票；数据不足的交易不能据此认定为无相关触限。</td></tr>'
    doc += ('</tbody><tfoot><tr><td colspan="11">当前展示股票已知金额合计（跨交易每份金额及比例不相加）</td>'
            f'<td>{total("gross_price_pnl", {"manager", "physical", "fixed", "unwound"})}</td>'
            f'<td>{total("etf_cost_impact")}</td><td>—</td><td>—</td></tr></tfoot></table>')
    doc += '<details><summary>每笔交易的检查结果（包括无相关事件及数据不足）</summary><table><thead><tr><th>交易编号</th><th>发起时间</th><th>方向</th><th>申购涨停／赎回跌停股票数</th><th>代码</th><th>无法判断股票数</th></tr></thead><tbody>'
    for r in coverage:
        doc += '<tr>' + ''.join(f'<td>{cell(v)}</td>' for v in (r["trade_id"], r["opened_at"],
            "申购" if r["side"] == "CREATE" else "赎回", r["limit_stock_count"], r["limit_symbols"] or "—",
            r["unknown_stock_count"])) + '</tr>'
    return doc + '</tbody></table></details></section>'
