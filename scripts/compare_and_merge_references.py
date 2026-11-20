"""Audit PCF stock dates against official market data. Add verified reference prices only with --merge."""
import argparse
from bisect import bisect_left
from collections import Counter, defaultdict
import csv
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

import pyarrow.dataset as ds
from collect_exchange_prices import (
    CACHE_NAME, FIELDS as QUOTE_FIELDS, atomic_output, common_arguments, read_page, read_selection,
    save_json, source_url,
)

CENT = Decimal("0.01")
COLUMNS = ["date", "symbol", "reference_price", "source_url"]
DETAIL = ["date", "symbol", "funds", "pcf_prior", "exchange_prior", "local_prior_source",
          "local_prior_close", "exchange_prior_close", "exchange_reference", "exchange_close", "status"]
CONFIRMED = {"equal_reference", "confirmed_adjustment"}

def write_csv(path, fields, rows):
    with atomic_output(path, "utf-8-sig") as out:
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

def yuan_price(value):
    try:
        result = Decimal(str(value))
        if result.is_finite() and result > 0 and result.quantize(CENT) == result:
            return result
    except InvalidOperation:
        pass
    return None

def read_local(root, by_day, supplement):
    local, conflicts = {}, set()
    def record(key, raw, source):
        price = yuan_price(raw)
        if price is None or (key in local and local[key][0] != price):
            conflicts.add(key)
        else:
            local.setdefault(key, (price, source))
    months = defaultdict(list)
    for day in by_day:
        months[day[:7]].append(day)
    for month, days in sorted(months.items()):
        files = list((root / month).glob("*.parquet"))
        if not files:
            continue
        times = [datetime.combine(date.fromisoformat(day), time(15)) for day in days]
        symbols = sorted({s for day in days for s in by_day[day]})
        table = ds.dataset([str(path) for path in files], format="parquet")
        filters = ds.field("time").isin(times) & ds.field("thscode").isin(symbols)
        for batch in table.to_batches(columns=["time", "thscode", "close"], filter=filters):
            for moment, symbol, value in zip(*(batch.column(i).to_pylist() for i in range(3))):
                if value is not None:
                    record((moment.date().isoformat(), symbol), value, "local_1500")
    if supplement.is_file():
        with supplement.open(encoding="utf-8-sig", newline="") as inp:
            for row in csv.DictReader(inp):
                key = row["previous_date"], row["symbol"]
                if key[1] in by_day.get(key[0], ()):
                    link = urlsplit(row["source_url"])
                    if link.scheme in {"http", "https"} and link.netloc:
                        record(key, row["close"], "sourced_prior")
    return local, conflicts

def symbol_history(symbol, ranges, pages, cache):
    bars = {}
    for lo, hi in ranges:
        file = pages.get((symbol, lo, hi))
        if file is None:
            return None
        match = CACHE_NAME.fullmatch(file)
        if not match or match[1] != symbol or not (match[2] <= lo <= hi <= match[3]):
            raise ValueError(f"Incompatible cached page: {file}")
        try:
            page = read_page(cache / file, symbol, exact=True)
        except (OSError, ValueError) as exc:
            print(f"Unavailable exchange page {file}: {exc}", file=sys.stderr)
            return None
        for raw_day, ref, opening, high, low, close, change, volume in page["kline"]:
            day = datetime.strptime(str(raw_day), "%Y%m%d").date().isoformat()
            if not match[2] <= day <= match[3]:
                raise ValueError(f"Exchange returned {day} outside requested cache page {file}")
            if not lo <= day <= hi:
                continue
            if day in bars:
                raise ValueError(f"Duplicate exchange session for {symbol} on {day}")
            # Only prevClose supplies the opening reference. Other daily fields cross-check the
            # historical record; never set the reference from today's close or OHLC bounds.
            prices = [yuan_price(v) for v in (ref, opening, high, low, close)]
            valid = all(v is not None for v in prices)
            if valid:
                ref, opening, high, low, close = prices
                valid = (isinstance(volume, (int, Decimal)) and volume > 0 and low <= opening <= high
                         and low <= close <= high and isinstance(change, (int, Decimal)) and close - change == ref)
            bars[day] = (ref, close, valid)
    return bars

def classify(row, fprior, symbol, day, bars, calendar, local, conflicts):
    if len(set(fprior.values())) != 1:
        return "inconsistent_pcf_prior"
    prior = row["pcf_prior"] = next(iter(fprior.values()))
    loc = local.get((prior, symbol))
    if loc:
        row["local_prior_close"], row["local_prior_source"] = loc
    if bars is None:
        return "exchange_response_unavailable"
    if day not in bars:
        return "no_exchange_bar_on_pcf_date"
    ref, close, valid = bars[day]
    row["exchange_reference"], row["exchange_close"] = ref, close
    if not valid:
        return "zero_volume_or_invalid_current_exchange_bar"
    i = bisect_left(calendar, day) - 1
    if i < 0:
        return "no_previous_stock_bar"
    previous = row["exchange_prior"] = calendar[i]
    _, last_close, last_valid = bars[previous]
    row["exchange_prior_close"] = last_close
    if previous != prior:
        return "prior_stock_session_gap"
    if not last_valid:
        return "zero_volume_or_invalid_previous_exchange_bar"
    if (prior, symbol) in conflicts:
        return "conflicting_local_close"
    if loc and loc[0] != last_close:
        return "local_and_exchange_previous_close_disagree"
    return "equal_reference" if ref == last_close else "confirmed_adjustment"

def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

def existing_prices(path):
    existing = {}
    if path.is_file():
        with path.open(encoding="utf-8-sig", newline="") as inp:
            reader = csv.DictReader(inp)
            if set(reader.fieldnames or []) != set(COLUMNS):
                raise ValueError(f"Unexpected existing feed columns: {path}")
            for row in reader:
                key = row["date"], row["symbol"]
                date.fromisoformat(key[0])
                url = urlsplit(row["source_url"])
                if (key in existing or yuan_price(row["reference_price"]) is None
                        or not CACHE_NAME.fullmatch(f"{key[1]}_{key[0]}_{key[0]}.json")
                        or url.scheme not in {"https", "http"} or not url.netloc):
                    raise ValueError(f"Duplicate or invalid existing reference at {path}:{reader.line_num}")
                existing[key] = row
    return existing

def merge_rows(target, original_hash, rows):
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = target.with_name(target.name + ".lock")
    lock.open("x").close()
    try:
        if file_hash(target) != original_hash:
            raise ValueError(f"Existing feed changed during reconciliation: {target}")
        write_csv(target, COLUMNS, (rows[key] for key in sorted(rows)))
    finally:
        lock.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    common_arguments(parser)
    parser.add_argument("--merge", action="store_true", help="Atomically add new verified prices to the feed")
    parser.add_argument("--target", type=Path, help="Override the configured reference feed path")
    args = parser.parse_args()
    config, start, end, dates, funds = read_selection(args)
    audit = args.audit_dir.expanduser().resolve()
    cache = (args.cache_dir or audit / "exchange_dayk").expanduser().resolve()
    root = config["minute_root"]
    if not root.is_dir():
        parser.error(f"Missing minute root: {root}")
    if not (audit / "pages.json").is_file() or not (audit / "membership.json").is_file():
        parser.error(f"Run collect_exchange_prices.py first for audit directory {audit}")
    manifest = json.loads((audit / "pages.json").read_text(encoding="utf-8"))
    raw = json.loads((audit / "membership.json").read_text(encoding="utf-8"))
    days_selected = {d.isoformat() for d in dates}
    if (manifest["select"] != QUOTE_FIELDS or Path(manifest["pcf_root"]) != config["pcf_root"]
            or not set(funds) <= set(manifest["funds"]) or not days_selected <= set(manifest["dates"])):
        parser.error("Cache manifest does not match selected dates, PCFs, or exchange fields")
    target = (args.target or config.get("adjusted_reference_prices_path") or root / "adjusted_reference_prices.csv").resolve()
    pages = {(s, lo, hi): filename for s, lo, hi, filename in manifest["pages"]}
    by_symbol, by_prior = defaultdict(lambda: defaultdict(dict)), defaultdict(set)
    pcf_days = Counter()
    for fund in funds:
        for day, (prior, symbols) in raw["funds"][fund].items():
            if day in days_selected:
                pcf_days[fund] += 1
                by_prior[prior].update(symbols)
                for symbol in symbols:
                    by_symbol[symbol][day][fund] = prior
    if not by_symbol:
        parser.error("No positive-quantity A-share components for this selection")
    local, conflicting = read_local(root, by_prior, config.get("report_previous_closes_path") or root / "report_prior_closes.csv")
    statuses, bases = Counter(), Counter()
    fund_statuses, fund_bases = defaultdict(Counter), defaultdict(Counter)
    changes, exceptions, added, checked = [], [], [], {}
    for symbol, trading_days in sorted(by_symbol.items()):
        bars = symbol_history(symbol, manifest["ranges"], pages, cache)
        calendar = sorted(bars or {})
        for day, fprior in sorted(trading_days.items()):
            row = dict(date=day, symbol=symbol, funds=";".join(sorted(fprior)))
            status = row["status"] = classify(row, fprior, symbol, day, bars, calendar, local, conflicting)
            statuses[status] += 1
            for fund in fprior:
                fund_statuses[fund][status] += 1
            if status not in CONFIRMED:
                exceptions.append(row)
                continue
            basis = row.get("local_prior_source", "exchange_prior_only")
            bases[basis] += 1
            for fund in fprior:
                fund_bases[fund][basis] += 1
            checked[day, symbol] = row["exchange_reference"]
            if status == "confirmed_adjustment":
                changes.append(row)
                added.append(dict(date=day, symbol=symbol, reference_price=format(row["exchange_reference"], ".2f"),
                                  source_url=source_url(symbol, day, day)))
    write_csv(audit / "confirmed_adjustments.csv", DETAIL, changes)
    write_csv(audit / "reference_exceptions.csv", DETAIL, exceptions)
    write_csv(audit / "exchange_adjusted_reference_prices.csv", COLUMNS, added)
    before = file_hash(target)
    existing = existing_prices(target)
    conflicts = [dict(date=key[0], symbol=key[1], existing_price=row["reference_price"], exchange_price=checked[key])
                 for key, row in existing.items() if key in checked and Decimal(row["reference_price"]) != checked[key]]
    write_csv(audit / "reference_conflicts.csv", ["date", "symbol", "existing_price", "exchange_price"], conflicts)
    new = {(r["date"], r["symbol"]): r for r in added if (r["date"], r["symbol"]) not in existing}
    if args.merge and new and not conflicts:
        merge_rows(target, before, {**existing, **new})
    gaps = [entry for entry in raw["gaps"] if entry[0] in funds and entry[1] in days_selected]
    summary = dict(start=start.isoformat(), end=end.isoformat(), all_unique_stock_days=sum(statuses.values()),
                   all_unique_stocks=len(by_symbol), all_status=statuses, pcf_days=pcf_days, pcf_gaps=gaps,
                   funds={f: {"stock_days": sum(fund_statuses[f].values()), **fund_statuses[f]} for f in funds},
                   comparison_basis={"all_unique": bases, "funds": fund_bases}, existing_rows=len(existing),
                   new_rows=len(new), conflicts=conflicts, write_enabled=args.merge, source_file=str(target),
                   written_rows=len(new) if args.merge and not conflicts else 0, source_sha256=file_hash(target))
    save_json(audit / "reference_coverage.json", summary)
    print(f"{sum(statuses.values())} stock dates, {len(added)} confirmed differences, {len(exceptions)} exceptions,"
          f" {len(conflicts)} conflicts, {summary['written_rows']} new rows written. Results: {audit}")
    return 1 if conflicts or gaps or statuses["exchange_response_unavailable"] else 0

if __name__ == "__main__":
    sys.exit(main())
