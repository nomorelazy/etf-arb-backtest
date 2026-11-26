"""Read-only, signal-time risk membership and basket value weights.

This is a user-defined notional exposure, not an expected loss or a cash flow.
No post-signal quote, fill, valuation or settlement selects a risk constituent.
"""

from collections import defaultdict
import csv
from datetime import datetime, time
from decimal import Decimal
from html import escape
import json
from pathlib import Path

import pyarrow.dataset as ds

from etf_arb.rules.suspensions import with_suspensions
from etf_arb.data.adjusted_references import AdjustedReferences
from etf_arb.data.pcf import FilePCFRepository
from etf_arb.data.snapshot import component_symbol
from etf_arb.pricing.errors import PricingDataError
from etf_arb.pricing.iopv import (SubstitutionKind, _normalize_substitution_flag,
                                fixed_substitution_amount)
from etf_arb.report.limit_impact import EventPrices
from etf_arb.report.price_limit_bounds import ReportPriceLimits
from etf_arb.report.prior_closes import PriorCloses, report_prior_path
from etf_arb.report.stock_cost import decimal, timestamp
from etf_arb.rules import InMemoryRuleRepository
from etf_arb.rules.errors import RuleNotFoundError
from etf_arb.runner_config import load_rules


ZERO = Decimal(0)
SELECTION_BASIS = "signal_time_directional_risk"
SCHEMA_VERSION = 2
COMPONENT_SCOPE = "exclude_cross_market_cash_substitution"
# Keep reporting compatible with Desktop variants without FULL_FLOW_ETFS.
ETF_MARKETS = {"159338": "SZ", "159516": "SZ", "512760": "SH",
               "512880": "SH", "515880": "SH"}
CATEGORIES = ("limit", "suspension", "mandatory_cash", "total")
LABELS = {"limit_up": "申购涨停", "limit_down": "赎回跌停",
          "suspension": "停牌", "mandatory_cash": "PCF必须现金替代"}
DETAIL_FIELDS = (
    "trade_id", "opened_at", "side", "symbol", "security_name", "pcf_quantity",
    "quantity", "etf_shares", "risk_tags", "signal_price", "signal_price_at",
    "valuation_basis", "signal_value", "basket_value", "etf_weight",
    "limit_status", "limit_source", "is_suspended", "suspension_source",
    "pcf_mandatory", "valuation_issue",
)
SUMMARY_FIELDS = (
    "trade_id", "opened_at", "side", "basket_value", "etf_shares", "component_count",
    "etf_market", "excluded_cross_market_cash_count",
    *(f"{name}_{field}" for name in CATEGORIES for field in
      ("stock_count", "value", "weight", "known_value", "known_weight", "missing_value_count", "unknown_stock_count")),
    "coverage_complete", "estimated_limit_stock_count", "unknown_details",
)


class RiskEventPrices(EventPrices):
    """Read only requested events, retaining bad report-only quotes as unknown.

    Fixed-cash constituents can have absent or unusable market bars without
    affecting execution. Reading them for this report must not invalidate the
    original backtest. A conflicting event is never resolved by row order.
    """

    def __init__(self, root):
        super().__init__(root)
        self.issues = {}

    def _invalidate(self, key, reason):
        reasons = set(self.issues.get(key, '').split('+')) - {''}
        reasons.add(reason)
        self.issues[key] = '+'.join(sorted(reasons))
        self.values.pop(key, None)

    def read(self, requests):
        months = defaultdict(lambda: defaultdict(set))
        for at, symbols in requests.items():
            months[at.strftime('%Y-%m')][at].update(symbols)
        for month, times in sorted(months.items()):
            files = sorted((self.root / month).glob('*.parquet'))
            if not files:
                continue
            symbols = set().union(*times.values())
            table = ds.dataset([str(path) for path in files], format='parquet').to_table(
                columns=['time', 'thscode', 'close'],
                filter=ds.field('time').isin(sorted(times)) & ds.field('thscode').isin(sorted(symbols)))
            for at, symbol, raw in zip(*(table[name].to_pylist() for name in ('time', 'thscode', 'close'))):
                if symbol not in times.get(at, ()):
                    continue
                key = symbol, at
                if raw is None:
                    self._invalidate(key, 'missing_close')
                    continue
                try:
                    price = decimal(raw)
                    if price is None or price <= ZERO:
                        raise ValueError('Nonpositive report quote')
                except (ArithmeticError, TypeError, ValueError):
                    self._invalidate(key, 'invalid_close')
                    continue
                if key in self.values and self.values[key] != price:
                    self._invalidate(key, 'conflicting_close')
                elif key not in self.issues:
                    self.values[key] = price


def _setting(config, name, default=None):
    return config.get(name, default) if isinstance(config, dict) else getattr(config, name, default)


def _etf_market(pcf, etf_symbol):
    market = ETF_MARKETS.get(pcf.etf_code)
    if market is None:
        raise ValueError(f"Unsupported ETF market for signal risk: {pcf.etf_code}")
    parts = etf_symbol.split(".")
    if parts[0] != pcf.etf_code or (len(parts) > 1 and parts != [pcf.etf_code, market]):
        raise ValueError(f"ETF market conflicts with PCF: {etf_symbol}/{pcf.etf_code}.{market}")
    return market


def is_current_signal_risk(data):
    """Do not reuse all-market snapshots or relabel their derived totals."""
    return (isinstance(data, dict) and data.get('schema_version') == SCHEMA_VERSION
            and data.get('selection_basis') == SELECTION_BASIS
            and data.get('mandatory_scope') == 'pcf_mandatory_only'
            and data.get('component_scope') == COMPONENT_SCOPE
            and isinstance(data.get('rows'), list) and isinstance(data.get('trades'), list))


def _require_current_snapshot(data):
    if not is_current_signal_risk(data):
        raise ValueError('Incompatible signal risk snapshot; rebuild from signal-time inputs')


def _security_rule(rules, symbol, code, day):
    for key in (symbol, code):
        try:
            return rules.get_security_rule(key, day)
        except RuleNotFoundError:
            pass
    return None


def _weight(value, basket):
    return value / basket if value is not None and basket is not None and basket > ZERO else None


def _summary(rows, unknown, category, basket):
    selected = rows if category == "total" else [row for row in rows if
        (any(tag in {"limit_up", "limit_down"} for tag in row["risk_tags"].split(";"))
         if category == "limit" else category in row["risk_tags"].split(";"))]
    values = [row["signal_value"] for row in selected if row["signal_value"] is not None]
    missing = len(selected) - len(values)
    known = sum(values, ZERO)
    value = known if not missing and not unknown else None
    return {f"{category}_{key}": item for key, item in dict(
        stock_count=len(selected), value=value, weight=_weight(value, basket),
        known_value=known, known_weight=_weight(known, basket),
        missing_value_count=missing, unknown_stock_count=len(unknown)).items()}


def build_signal_risk(config, trades, signals, *, rules=None):
    """Build trade-time snapshots in one batched minute read per required month."""
    data = dict(schema_version=SCHEMA_VERSION, selection_basis=SELECTION_BASIS,
        component_scope=COMPONENT_SCOPE,
        mandatory_scope="pcf_mandatory_only", price_basis="exact_signal_minute_or_pcf_fixed_cash",
        weight_basis="signal_iopv_times_etf_shares", rows=[], trades=[])
    if not trades:
        return data
    if rules is None:
        path = _setting(config, "rules_path")
        rules = load_rules(path) if path else InMemoryRuleRepository()
    rules = with_suspensions(config, rules)
    pcfs = FilePCFRepository(_setting(config, "pcf_root"))
    contexts, cache, requests = {}, {}, defaultdict(set)
    signal_map = {timestamp(row["ts"]): row for row in signals}
    ids = set()
    for trade in trades:
        tid = trade["trade_id"]
        if tid in ids:
            raise ValueError(f"Duplicate risk trade id: {tid}")
        ids.add(tid)
        at, units = timestamp(trade["opened_at"]), int(trade["quantity_units"])
        if units <= 0 or trade["side"] not in {"CREATE", "REDEEM"}:
            raise ValueError("Risk report requires positive units and CREATE/REDEEM side")
        etf_symbol = trade.get("etf_symbol") or _setting(config, "etf_codes")[0]
        code = etf_symbol.split(".")[0]
        key = code, at.date()
        if key not in cache:
            cache[key] = pcfs.get(*key)
        pcf = cache[key]
        market = _etf_market(pcf, etf_symbol)
        components = {}
        seen, excluded = set(), 0
        for component in pcf.components:
            if component.is_cash_placeholder:
                continue
            try:
                substitution = _normalize_substitution_flag(component.substitution_flag)
                mandatory = substitution == SubstitutionKind.MANDATORY
            except PricingDataError:
                substitution = None
                mandatory = None
            if component.quantity == 0 and mandatory is False:
                continue
            symbol = component_symbol(component)
            if symbol in seen:
                raise ValueError(f"Duplicate PCF constituent: {code} {at.date()} {symbol}")
            seen.add(symbol)
            if (symbol.rsplit(".", 1)[-1] != market
                    and substitution in {SubstitutionKind.ALLOWED, SubstitutionKind.MANDATORY}):
                excluded += 1
                continue
            components[symbol] = component, mandatory
            requests[at].add(symbol)
            if pcf.pre_trading_day and pcf.pre_trading_day < at.date():
                requests[datetime.combine(pcf.pre_trading_day, time(15))].add(symbol)
        contexts[tid] = pcf, components, at, units, market, excluded
    prices = RiskEventPrices(_setting(config, "minute_root"))
    prices.read(requests)
    data['quote_issue_count'] = len(prices.issues)
    bounds_path = _setting(config, "daily_price_limits_path")
    limits = ReportPriceLimits(prices, _setting(config, "minute_root"), Path(bounds_path) if bounds_path else None,
                              adjusted_references=AdjustedReferences.from_config(config))
    limits.previous = PriorCloses(prices, report_prior_path(config))
    data["prior_closes_sha256"] = limits.previous.sha256
    data["stock_suspensions_sha256"] = getattr(getattr(rules, "suspensions", None), "sha256", None)
    data["adjusted_references_sha256"] = limits.adjusted.sha256
    bounds_by_day = {}
    for trade in sorted(trades, key=lambda row: (timestamp(row["opened_at"]), row["trade_id"])):
        tid = trade["trade_id"]
        pcf, components, at, units, market, excluded = contexts[tid]
        key = pcf.etf_code, at.date()
        if key not in bounds_by_day:
            limits.on_day_open(pcf, rules, check_basket=False,
                               held_components=[item[0] for item in components.values()])
            bounds_by_day[key] = dict(limits.bounds)
        limits.bounds = bounds_by_day[key]
        shares = decimal(trade.get("etf_shares"))
        iopv = decimal(signal_map.get(at, {}).get("iopv"))
        basket = iopv * shares if iopv is not None and iopv > ZERO and shares is not None and shares > ZERO else None
        unknown = {name: set() for name in CATEGORIES}
        details, selected_rows = [], []
        estimated = 0
        for symbol, (component, mandatory) in sorted(components.items()):
            tags = []
            rule = _security_rule(rules, symbol, component.security_code, at.date())
            feed = getattr(rules, "suspensions", None)
            notice = feed.on(symbol, at.date()) if feed is not None else None
            suspended = rule.is_suspended if rule is not None else None
            status = limits.status(symbol, at)
            quote_issue = prices.issues.get((symbol, at))
            if quote_issue:
                status = 'unknown:' + quote_issue
            elif status == 'unknown:missing_previous_close' and pcf.pre_trading_day:
                previous_issue = prices.issues.get((symbol, datetime.combine(pcf.pre_trading_day, time(15))))
                if previous_issue:
                    status = 'unknown:reference_' + previous_issue
            kind, source = status.split(":", 1) if status else ("normal", limits.bounds.get(symbol, (None, "unknown"))[1])
            if source in {"estimated", "estimated_open_0930_proxy", "adjusted_reference_price"}:
                estimated += 1
            relevant = "limit_up" if trade["side"] == "CREATE" else "limit_down"
            if kind == relevant:
                tags.append(kind)
            elif kind == "unknown":
                unknown["limit"].add(symbol)
                details.append(f"{symbol}:涨跌停未确定({source})")
            if suspended is True:
                tags.append("suspension")
            elif suspended is None:
                unknown["suspension"].add(symbol)
                details.append(f"{symbol}:停牌状态未提供")
            if mandatory is True:
                tags.append("mandatory_cash")
            elif mandatory is None:
                unknown["mandatory_cash"].add(symbol)
                details.append(f"{symbol}:现金替代标志未确定")
            if not tags:
                if any(symbol in unknown[name] for name in CATEGORIES[:-1]):
                    unknown["total"].add(symbol)
                continue
            bar = prices.get(symbol, at, exact=True)
            quantity = component.quantity * units
            issue = ""
            if mandatory is None:
                basis = "unknown_substitution_flag"
                value, issue = None, "unknown_substitution_flag"
            elif mandatory:
                basis = "pcf_creation_fixed_cash"
                try:
                    value = fixed_substitution_amount(component) * units
                except PricingDataError:
                    value, issue = None, "missing_or_invalid_fixed_cash"
            else:
                basis = "exact_signal_minute_close"
                value = bar.close * quantity if bar is not None else None
                if value is None:
                    issue = quote_issue or "missing_exact_signal_close"
            row = dict(trade_id=tid, opened_at=at, side=trade["side"], symbol=symbol,
                security_name=component.security_name or "", pcf_quantity=component.quantity,
                quantity=quantity, etf_shares=shares, risk_tags=";".join(tags),
                signal_price=bar.close if bar else None, signal_price_at=bar.time if bar else None,
                valuation_basis=basis, signal_value=value, basket_value=basket,
                etf_weight=_weight(value, basket), limit_status=kind, limit_source=source,
                is_suspended=suspended, suspension_source=(notice.source_url if notice else "provided_security_rule" if rule else "missing_security_rule"),
                pcf_mandatory=mandatory, valuation_issue=issue)
            selected_rows.append(row)
        data["rows"].extend(selected_rows)
        summary = dict(trade_id=tid, opened_at=at, side=trade["side"], basket_value=basket,
            etf_market=market, excluded_cross_market_cash_count=excluded,
            etf_shares=shares, component_count=len(components), estimated_limit_stock_count=estimated,
            unknown_details=";".join(details))
        for category in CATEGORIES:
            summary.update(_summary(selected_rows, unknown[category], category, basket))
        summary["coverage_complete"] = (summary["total_value"] is not None and basket is not None)
        data["trades"].append(summary)
    return data


def signal_risk_html(data, *, trade_id=None):
    """Render all rows without inner scrolling; percentages are never added across trades."""
    _require_current_snapshot(data)
    trades = [row for row in data["trades"] if trade_id is None or row["trade_id"] == trade_id]
    rows = [row for row in data["rows"] if trade_id is None or row["trade_id"] == trade_id]
    def number(value):
        return "未确定" if value in (None, "") else f"{decimal(value):,.2f}"
    def percent(value):
        return "未确定" if value in (None, "") else f"{decimal(value):.4%}"
    def text(value):
        return escape(str(value if value is not None else ""))
    def amount(row, category):
        missing = int(row[f"{category}_missing_value_count"])
        unknown = int(row[f"{category}_unknown_stock_count"])
        partial = bool(missing or unknown)
        prefix = "已识别 " if partial else ""
        result = f'{prefix}{number(row[f"{category}_known_value"])} 元 / {percent(row[f"{category}_known_weight"])}'
        if partial:
            result += f"<br><small>{missing} 只缺估值，{unknown} 只待识别</small>"
        return result
    unknown_trades = sum(not bool(row["coverage_complete"]) for row in trades)
    doc = '<section class="signal-risk"><h2>信号时风险敞口</h2>'
    doc += ('<p>按 ETF 上市市场区分本市场与跨市场：排除跨市场允许／必须现金替代股票，保留本市场股票（含现金替代）。'
            '159338、159516 的本市场为深市；512760、512880、515880 的本市场为沪市。'
            '跨市场禁止替代或替代标志未知的股票不擅自按现金代理排除。'
            '在此范围内，按发出套利信号的 t 分钟统计：申购涨停、赎回跌停、停牌及 PCF 标为必须现金替代的真实成分股。'
            '占比＝该股信号篮子估值÷（信号 IOPV×本笔 ETF 份数）。必须现金替代使用当日 PCF 固定申购金额作为统一比较基准，包括数量为零的固定现金行；'
            '不计虚拟申赎现金汇总行。分母仍为整篮 ETF 信号估值，不因排除跨市场现金替代而缩小。</p>'
            '<p>三类可重叠，总敞口按股票去重；金额为按上述分类的信号时名义敞口，不是实时净风险、预计亏损或新增现金扣款。固定现金替代金额也不等同于尚未锁价的股价风险。'
            '信号缺价不取前一分钟或未来价格补值；缺停牌资料不等于未停牌。前收盘估算与 09:30 close 开盘代理估算均计入估算限价数量。'
            '开盘代理仅在缺少原可算限价时使用当日 09:30 精确 close；这不是真实开盘价或交易所限价。'
            '缺少该分钟价格仍为未确定；触及估算限价不代表盘口封单。</p>')
    doc += f'<p>共 {len(trades)} 笔，{unknown_trades} 笔敞口覆盖或估值未完整；下表“已识别”数值仅为已知部分。百分比不跨交易合计。</p>'
    doc += '<table style="width:100%;table-layout:fixed;overflow-wrap:anywhere"><thead><tr>'
    doc += ''.join(f'<th>{heading}</th>' for heading in ('交易／信号时间', '方向', '本市场口径涨跌停敞口／占比', '停牌敞口／占比', '必须现金替代／占比', '去重总敞口／占比', '股票数／识别覆盖'))
    doc += '</tr></thead><tbody>'
    for row in trades:
        doc += f'<tr><td>{text(row["trade_id"])}<br>{text(row["opened_at"])}</td><td>{"申购" if row["side"] == "CREATE" else "赎回"}</td>'
        doc += ''.join(f'<td>{amount(row, category)}</td>' for category in CATEGORIES)
        doc += f'<td>{row["total_stock_count"]} 只风险股 / {row["component_count"]} 只范围内成分股<br>已排除跨市场现金替代 {row["excluded_cross_market_cash_count"]} 只<br>停牌状态缺失 {row["suspension_unknown_stock_count"]} 只<br>估算限价 {row["estimated_limit_stock_count"]} 只</td></tr>'
    doc += '</tbody></table><h3>风险成分股明细</h3>'
    if not rows:
        return doc + '<p>未识别出符合上述条件的风险成分股；资料不完整时不代表敞口为零。</p></section>'
    doc += '<table style="width:100%;table-layout:fixed;overflow-wrap:anywhere"><thead><tr>'
    doc += ''.join(f'<th>{heading}</th>' for heading in ('交易／信号时间', '股票／方向', '风险类别', '本笔股数', '信号价／估值口径', '风险金额（元）', '占ETF信号篮子', '涨跌停依据'))
    doc += '</tr></thead><tbody>'
    for row in rows:
        tags = '、'.join(LABELS[tag] for tag in row['risk_tags'].split(';'))
        basis = {'pcf_creation_fixed_cash': 'PCF固定金额',
                 'exact_signal_minute_close': '信号分钟价格×股数'}.get(row['valuation_basis'], '替代标志未确定')
        source = {'provided': '提供的限价', 'estimated': '前收盘估算',
                  'adjusted_reference_price': '除权除息参考价估算', 'stock_suspended': '公告停牌',
                  'estimated_open_0930_proxy': '09:30 close 开盘代理估算'}.get(row['limit_source'], '未确定')
        notice = str(row.get("suspension_source") or "")
        if row.get("is_suspended") and notice.startswith(("https://", "http://")):
            source += f' <a href="{escape(notice, quote=True)}">停牌公告</a>'
        doc += (f'<tr><td>{text(row["trade_id"])}<br>{text(row["opened_at"])}</td>'
            f'<td>{text(row["symbol"])} {text(row["security_name"])}<br>{"申购" if row["side"] == "CREATE" else "赎回"}</td>'
            f'<td>{text(tags)}</td><td>{number(row["quantity"])}</td><td>{number(row["signal_price"])} / {basis}</td>'
            f'<td>{number(row["signal_value"])}</td><td>{percent(row["etf_weight"])}</td><td>{source}</td></tr>')
    return doc + '</tbody></table></section>'


def save_signal_risk(output_dir, data):
    _require_current_snapshot(data)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'signal_risk.json').write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    for filename, key, fields in (('trade_signal_risk.csv', 'trades', SUMMARY_FIELDS),
                                  ('stock_signal_risk.csv', 'rows', DETAIL_FIELDS)):
        with (output / filename).open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(data[key])
    (output / 'signal_risk.html').write_text(
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>信号时风险敞口</title>'
        '<style>body{font:14px sans-serif;color:#16324f;padding:24px}table{border-collapse:collapse;margin:14px 0}th,td{border:1px solid #ddd;padding:8px;vertical-align:top}th{background:#f2f5f9}small{color:#666}</style><body>'
        + signal_risk_html(data) + '</body></html>', encoding='utf-8')


def load_signal_risk(path):
    """Only accept this exact signal-time definition; older attribution is not a substitute."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    _require_current_snapshot(data)
    return data
