"""Signal-minute stock market values and weights for the standard ETF report."""

from collections import Counter, defaultdict
from datetime import datetime, time
from decimal import Decimal
import json
import os
from tempfile import NamedTemporaryFile
from pathlib import Path

from etf_arb.data.pcf import FilePCFRepository
from etf_arb.data.snapshot import component_symbol
from etf_arb.pricing.errors import PricingDataError
from etf_arb.pricing.iopv import SubstitutionKind, _normalize_substitution_flag
from etf_arb.report.price_limit_bounds import ReportPriceLimits
from etf_arb.report.prior_closes import PriorCloses, prior_closes_match, report_prior_path
from etf_arb.report.signal_risk import RiskEventPrices, _etf_market, _setting
from etf_arb.report.stock_cost import decimal, timestamp
from etf_arb.rules import InMemoryRuleRepository
from etf_arb.rules.limit_reference import OPENING_PROXY_SOURCE, OPENING_PROXY_TIME, opening_proxy_is_future
from etf_arb.runner_config import load_rules


ZERO = Decimal(0)
METRICS = ("count", "known_count", "value", "known_value", "weight", "known_weight", "missing_value_count")


def _ratio(value, denominator):
    if value is not None and denominator is not None and denominator > 0:
        return value / denominator
    return None


def _metrics(hits, unknown_count, basket):
    if unknown_count is None:
        return dict.fromkeys(METRICS) | {"complete": False, "weight_complete": False}
    known = sum((s["value"] for s in hits if s["value"] is not None), ZERO)
    missing = sum(s["value"] is None for s in hits)
    complete = not missing and not unknown_count
    pct = _ratio(known, basket)
    return dict(count=len(hits) if complete else None, known_count=len(hits),
                value=known if complete else None, known_value=known,
                weight=pct if complete else None, known_weight=pct,
                missing_value_count=missing, complete=complete, weight_complete=complete and pct is not None)


def _coverage(count, unknown):
    identified = count - unknown if count is not None else None
    return dict(component_count=count, identified_count=identified, unknown_count=unknown,
                coverage=_ratio(Decimal(identified), count) if identified is not None else None)


def _constituents(pcf, market):
    result = {}
    for c in pcf.components:
        if c.is_cash_placeholder or c.quantity <= 0:
            continue
        symbol = component_symbol(c)
        if symbol in result:
            if result[symbol][0] == c:
                continue
            raise ValueError(f"Conflicting PCF stock {symbol} in {pcf.etf_code} {pcf.effective_date}")
        try:
            cash = _normalize_substitution_flag(c.substitution_flag) in {
                SubstitutionKind.ALLOWED, SubstitutionKind.MANDATORY}
        except PricingDataError:
            cash = False  # Only an identified cross-market cash proxy may be excluded.
        local = symbol.rsplit(".", 1)[1] == market or not cash
        result[symbol] = c, local
    return result


def _reference(prices, previous, pcf, symbol, source):
    if source == "estimated":
        at = datetime.combine(pcf.pre_trading_day, time(15))
        bar = previous.get(symbol, at, exact=True)
        url = None
        if prices.get(symbol, at, exact=True) is not None:
            label = "local_exact_previous_close"
        else:
            label = "verified_historical_previous_close_feed"
            url = previous.rows[symbol, at][1]
        return dict(reference_at=at.isoformat(" "), reference_price=bar.close, reference_source=label,
                    reference_url=url)
    if source == OPENING_PROXY_SOURCE:
        at = datetime.combine(pcf.effective_date, OPENING_PROXY_TIME)
        bar = prices.get(symbol, at, exact=True)
        return dict(reference_at=at.isoformat(" "), reference_price=bar.close,
                    reference_source="same_day_0930_close_proxy", reference_url=None)
    return dict(reference_at=None, reference_price=None, reference_source=source, reference_url=None)


def _snapshot(row, basket, limits, prices):
    at = timestamp(row["opened_at"])
    scope, refs, pcf = basket
    direction = "up" if row["side"] == "CREATE" else "down"
    hits, unknowns = [], []
    sources, ref_sources = Counter(), Counter()
    valid_price_count = 0
    for symbol, (component, local) in scope.items():
        bounds, source = limits.bounds[symbol]
        ref = refs[symbol]
        sources[source] += 1
        ref_sources[ref["reference_source"]] += 1
        bar = prices.get(symbol, at, exact=True)
        valid_price_count += bar is not None
        status = limits.status(symbol, at)
        issue = prices.issues.get((symbol, at))
        if issue:
            status = "unknown:" + issue
        if status is None:
            continue
        kind, _, why = status.partition(":")
        side = {"limit_up": "up", "limit_down": "down", "unknown": "unknown"}[kind]
        if opening_proxy_is_future(source, at):
            bounds = None
            ref = dict(reference_at=None, reference_price=None, reference_source=source, reference_url=None)
        price = bar.close if bar is not None else None
        quantity = component.quantity * row["quantity_units"]
        stock = dict(symbol=symbol, name=component.security_name or "", market=symbol.rsplit(".", 1)[1],
                     pcf_quantity=component.quantity, quantity=quantity, substitution_flag=component.substitution_flag,
                     price=price, price_at=at.isoformat(" ") if bar else None,
                     value=quantity * price if price is not None else None,
                     limit_side=side, local_scope=local, local_directional=(local and side == direction)
                     if side != "unknown" else None, source=source, reason=why if side == "unknown" else "",
                     upper_limit=bounds[0] if bounds else None, lower_limit=bounds[1] if bounds else None, **ref)
        (unknowns if side == "unknown" else hits).append(stock)
    hits.sort(key=lambda s: (s["limit_side"] != "up", s["symbol"]))
    unknowns.sort(key=lambda s: s["symbol"])
    count = len(scope)
    local_count = sum(local for _, local in scope.values())
    unknown_count, local_unknown = len(unknowns), sum(s["local_scope"] for s in unknowns)
    basket_value = row["basket_value"]
    all_scope = {side: _metrics([s for s in hits if s["limit_side"] == side], unknown_count, basket_value)
                 for side in ("up", "down")}
    all_scope.update(total=_metrics(hits, unknown_count, basket_value), **_coverage(count, unknown_count),
                     valid_price_count=valid_price_count, price_coverage=_ratio(Decimal(valid_price_count), count),
                     limit_source_counts=dict(sources), reference_source_counts=dict(ref_sources))
    local_scope = _metrics([s for s in hits if s["local_directional"]], local_unknown, basket_value)
    local_scope.update(**_coverage(local_count, local_unknown), excluded_cross_market_count=count-local_count,
                       limit_side=direction, market_only=all(s.rsplit(".", 1)[1] == row["etf_market"]
                       for s, (_, local) in scope.items() if local))
    row.update(all=all_scope, local_directional=local_scope, hit_stocks=hits, unknown_stocks=unknowns,
               zero_quantity_rows_excluded=sum(not c.is_cash_placeholder and c.quantity == 0 for c in pcf.components))


def _missing_snapshot(row):
    missing = _metrics([], None, row["basket_value"])
    cover = _coverage(None, None)
    row.update(coverage_issue="missing_pcf", hit_stocks=[], unknown_stocks=[], zero_quantity_rows_excluded=None,
               all=dict(up=missing, down=missing, total=missing, valid_price_count=None, price_coverage=None,
                        limit_source_counts={}, reference_source_counts={}, **cover),
               local_directional=dict(**missing, **cover, excluded_cross_market_count=None, market_only=None,
                                      limit_side="up" if row["side"] == "CREATE" else "down"))


def _signal_values(config, trades, signals):
    codes = _setting(config, "etf_codes", ())
    if not codes:
        codes = sorted({t["etf_symbol"].split(".")[0] for t in trades if t.get("etf_symbol")})
    single_code = codes[0].split(".")[0] if len(codes) == 1 else None
    times = {timestamp(t["opened_at"]) for t in trades}
    by_signal = {}
    for s in signals:
        symbol = s.get("etf_symbol") or s.get("etf_code") or single_code
        at = timestamp(s["ts"])
        if not symbol or at not in times:
            continue
        key = symbol.split(".")[0], at
        value = decimal(s.get("iopv"))
        if key in by_signal and by_signal[key] != value:
            by_signal[key] = None
        else:
            by_signal[key] = value
    return single_code, by_signal


def build_limit_value(config, trades, signals, *, rules=None, prices=None, prior_closes_path=None):
    """Read exact signal and reference prices in monthly batches; never change execution or PnL."""
    trades = sorted(trades, key=lambda r: (timestamp(r["opened_at"]), r["trade_id"]))
    single_code, by_signal = _signal_values(config, trades, signals)
    rows, days, requests, ids = [], {}, defaultdict(set), set()
    if trades:
        pcfs = FilePCFRepository(_setting(config, "pcf_root"))
    for trade in trades:
        tid, at, side = trade["trade_id"], timestamp(trade["opened_at"]), trade["side"]
        units = decimal(trade.get("quantity_units"))
        if tid in ids or units is None or units <= 0 or units != units.to_integral_value() or side not in {"CREATE", "REDEEM"}:
            raise ValueError(f"Invalid or duplicate limit-value trade: {tid}")
        ids.add(tid)
        symbol = trade.get("etf_symbol") or single_code
        if not symbol:
            raise ValueError(f"Missing ETF symbol for trade {tid}")
        code = symbol.split(".")[0]
        key = code, at.date()
        if key not in days:
            try:
                pcf = pcfs.get(*key)
                market = _etf_market(pcf, symbol)
                days[key] = pcf, market, _constituents(pcf, market), []
            except FileNotFoundError:
                days[key] = None
        day = days[key]
        shares, iopv = decimal(trade.get("etf_shares")), by_signal.get((code, at))
        basket_value = iopv * shares if iopv and iopv > 0 and shares and shares > 0 else None
        row = dict(trade_id=tid, opened_at=at.isoformat(" "), side=side, state=trade.get("state"),
                   etf_symbol=symbol, etf_market=None, etf_shares=shares, quantity_units=units, iopv=iopv,
                   basket_value=basket_value, coverage_issue=None)
        rows.append(row)
        if day is None:
            _missing_snapshot(row)
            continue
        pcf, market, scope, trades_today = day
        row["etf_market"] = _etf_market(pcf, symbol)
        trades_today.append(row)
        requests[at].update(scope)
        if len(trades_today) == 1:
            requests[datetime.combine(at.date(), OPENING_PROXY_TIME)].update(scope)
            if pcf.pre_trading_day and pcf.pre_trading_day < at.date():
                requests[datetime.combine(pcf.pre_trading_day, time(15))].update(scope)

    feed_sha256 = None
    if any(days.values()):
        minute_root = Path(_setting(config, "minute_root"))
        feed = report_prior_path(config, prior_closes_path)
        if rules is None:
            path = _setting(config, "rules_path")
            rules = load_rules(path) if path else InMemoryRuleRepository()
        if prices is None:
            prices = RiskEventPrices(minute_root)
        prices.read(requests)
        bounds_path = _setting(config, "daily_price_limits_path")
        limits = ReportPriceLimits(prices, minute_root, Path(bounds_path) if bounds_path else None)
        previous = PriorCloses(prices, feed)
        feed_sha256 = previous.sha256
        limits.previous, limits.opening = previous, prices
        for day in days.values():
            if day is None:
                continue
            pcf, market, scope, trades_today = day
            limits.on_day_open(pcf, rules)
            refs = {symbol: _reference(prices, previous, pcf, symbol, source)
                    for symbol, (_, source) in limits.bounds.items()}
            for row in trades_today:
                _snapshot(row, (scope, refs, pcf), limits, prices)
    sources, resource_counts = Counter(), Counter()
    for row in rows:
        sources.update(row["all"]["limit_source_counts"])
        resource_counts.update(row["all"]["reference_source_counts"])
    summary = dict(trade_count=len(rows), all_scope_complete_trades=sum(r["all"]["total"]["complete"] for r in rows),
                   local_scope_complete_trades=sum(r["local_directional"]["complete"] for r in rows),
                   missing_iopv_trades=sum(r["iopv"] is None for r in rows),
                   missing_pcf_trades=sum(r["coverage_issue"] == "missing_pcf" for r in rows),
                   stock_observations=sum(sources.values()), source_counts=dict(sources),
                   reference_source_counts=dict(resource_counts))
    return dict(schema_version=1, selection_basis="signal_minute_limit_stock_values",
                rows=rows, summary=summary, prior_closes_sha256=feed_sha256)


def save_limit_value(output, data):
    """Atomically replace the run's snapshot, including every trade and identification gap."""
    path = Path(output) / "limit_value.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temp = Path(handle.name)
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, default=str, separators=(",", ":"))
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return path


def _same_trades(data, config, trades, signals):
    if (not isinstance(data, dict) or data.get("schema_version") != 1
            or data.get("selection_basis") != "signal_minute_limit_stock_values"
            or not isinstance(data.get("rows"), list) or not isinstance(data.get("summary"), dict)
            or len(trades) != len(data["rows"])):
        return False
    single_code, iopvs = _signal_values(config, trades, signals)
    saved = {r["trade_id"]: r for r in data["rows"]}
    if len(saved) != len(trades) or set(saved) != {t["trade_id"] for t in trades}:
        return False
    for trade in trades:
        row = saved[trade["trade_id"]]
        symbol = trade.get("etf_symbol") or single_code
        at = timestamp(trade["opened_at"])
        shares = decimal(trade.get("etf_shares"))
        iopv = iopvs.get((symbol.split(".")[0], at)) if symbol else None
        basket = shares*iopv if shares and shares > 0 and iopv and iopv > 0 else None
        if (timestamp(row["opened_at"]) != at or symbol != row["etf_symbol"] or trade["side"] != row["side"]
                or decimal(trade.get("quantity_units")) != decimal(row["quantity_units"])
                or shares != decimal(row["etf_shares"]) or iopv != decimal(row["iopv"])
                or basket != decimal(row["basket_value"])):
            return False
    return True


def limit_value_from_run(run_dir, config, trades, signals):
    """Reuse only this run's matching snapshot. Rebuilding does not write to the source run."""
    trades, signals = list(trades), list(signals)
    run_dir = Path(run_dir)
    for path in (run_dir / "limit_value.json", run_dir / "report" / "limit_value.json"):
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if _same_trades(data, config, trades, signals) and prior_closes_match(data, config):
                return data
        except (ValueError, TypeError, KeyError, ArithmeticError):
            continue
    return build_limit_value(config, trades, signals)
