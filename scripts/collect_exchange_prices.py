"""Collect official, unadjusted SSE market-data pages for the selected ETF PCFs."""
import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, timedelta
import json
import os
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile
from threading import Lock
import time
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from etf_arb.data.pcf import FilePCFRepository, PCFParseError
from etf_arb.data.snapshot import component_symbol

FIELDS = "date,prevClose,open,high,low,close,change,volume"
CACHE_NAME = re.compile(r"^(\d{6}\.(?:SH|SZ))_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.json$")
A_SHARE = re.compile(r"(?:(?:60\d|68[89])\d{3}\.SH|(?:00[0-3]|30[01])\d{3}\.SZ)")

def source_url(symbol, start, end):
    code, exchange = symbol.split(".")
    return (f"https://yunhq.sse.com.cn:32042/v1/{exchange.lower()}1/dayk/{code}"
            f"?begin={start.replace('-', '')}&end={end.replace('-', '')}&select={FIELDS}")

@contextmanager
def atomic_output(path, encoding):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = NamedTemporaryFile("w", dir=path.parent, delete=False, encoding=encoding, newline="")
    try:
        with tmp:
            yield tmp
        os.replace(tmp.name, path)
    finally:
        Path(tmp.name).unlink(missing_ok=True)

def save_json(path, obj):
    with atomic_output(path, "utf-8") as out:
        json.dump(obj, out, ensure_ascii=False, separators=(",", ":"))

def common_arguments(parser):
    parser.add_argument("--config", required=True, type=Path, help="Backtest JSON configuration")
    parser.add_argument("--audit-dir", required=True, type=Path, help="Directory for membership and audit outputs")
    parser.add_argument("--cache-dir", type=Path, help="Exchange JSON cache (default AUDIT_DIR/exchange_dayk)")
    parser.add_argument("--etf", nargs="+", help="ETF codes (default etf_codes in config)")
    parser.add_argument("--start", type=date.fromisoformat, help="First PCF date (default config start_date)")
    parser.add_argument("--end", type=date.fromisoformat, help="Last PCF date (default config end_date)")

def read_selection(args):
    config_path = args.config.expanduser().resolve()
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    for key in ("pcf_root", "minute_root", "report_previous_closes_path", "adjusted_reference_prices_path"):
        if config.get(key):
            config[key] = (config_path.parent / Path(config[key]).expanduser()).resolve()
    raw_days = config["trading_days"]
    if isinstance(raw_days, str):
        raw_days = [line.strip() for line in (config_path.parent / raw_days).read_text(encoding="utf-8-sig").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    calendar = [date.fromisoformat(day) for day in raw_days]
    if calendar != sorted(set(calendar)):
        raise ValueError("trading_days must be an increasing, unique calendar")
    start = args.start or date.fromisoformat(config["start_date"])
    end = args.end or date.fromisoformat(config["end_date"])
    dates = [day for day in calendar if start <= day <= end]
    if not dates:
        raise ValueError("No configured trading days in the selected interval")
    funds = list(dict.fromkeys(args.etf or config["etf_codes"]))
    if not funds or any(not re.fullmatch(r"\d{6}", f) for f in funds):
        raise ValueError("Expected six-digit ETF codes")
    return config, start, end, dates, funds

def read_membership(pcf_root, dates, funds):
    # Each fund gets its own repository to bound the parser's in-memory PCF cache.
    groups, gaps = {}, []
    for fund in funds:
        repo = FilePCFRepository(pcf_root)
        groups[fund] = {}
        for day in dates:
            try:
                pcf = repo.get(fund, day)
                if pcf.pre_trading_day is None or pcf.pre_trading_day >= day:
                    raise ValueError("Missing or incompatible PCF prior date")
                symbols = {component_symbol(c) for c in pcf.components if c.quantity > 0 and not c.is_cash_placeholder}
                groups[fund][day.isoformat()] = [pcf.pre_trading_day.isoformat(),
                                                 sorted(s for s in symbols if A_SHARE.fullmatch(s))]
            except (FileNotFoundError, ValueError, PCFParseError) as exc:
                gaps.append([fund, day.isoformat(), str(exc)])
    return groups, gaps

def periods(first, last):
    while first <= last:
        # At most 465 weekdays, within the endpoint's 500-row page size.
        end = min(last, first + timedelta(days=650))
        yield first.isoformat(), end.isoformat()
        first = end + timedelta(days=1)

def available_pages(cache):
    result = defaultdict(list)
    for path in sorted(cache.glob("*.json")):
        match = CACHE_NAME.fullmatch(path.name)
        if match:
            symbol, lo, hi = match.groups()
            result[symbol].append((lo, hi, path.name))
    return result

def read_page(path, symbol, *, exact=False):
    """Decode decimal prices without a float conversion during reconciliation."""
    from decimal import Decimal
    with path.open(encoding="utf-8") as data:
        page = json.load(data, parse_float=Decimal if exact else float)
    validate_page(page, symbol)
    return page

def validate_page(page, symbol):
    if not isinstance(page, dict) or page.get("code") != symbol[:6] or not isinstance(page.get("kline"), list):
        raise ValueError(f"Unexpected exchange payload for {symbol}")
    if any(not isinstance(row, list) or len(row) != 8 for row in page["kline"]):
        raise ValueError(f"Unexpected exchange fields for {symbol}")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    common_arguments(parser)
    parser.add_argument("--cache-only", action="store_true", help="Read cached pages only, without network access")
    args = parser.parse_args()
    config, start, end, dates, funds = read_selection(args)
    audit = args.audit_dir.expanduser().resolve()
    cache = (args.cache_dir or audit / "exchange_dayk").expanduser().resolve()
    if not config["pcf_root"].is_dir():
        parser.error(f"Missing PCF root: {config['pcf_root']}")
    groups, gaps = read_membership(config["pcf_root"], dates, funds)
    stocks = sorted({s for d in groups.values() for _, symbols in d.values() for s in symbols})
    save_json(audit / "membership.json", {"funds": groups, "gaps": gaps})
    if not stocks:
        parser.error("No A-share PCF membership in the selected dates; inspect membership.json")
    first = min(start - timedelta(days=31),
                min(date.fromisoformat(prior) for d in groups.values() for prior, _ in d.values()))
    ranges = list(periods(first, end))
    choices = available_pages(cache)
    slots_lock = Lock()
    next_slot = 0.0

    def collect(job):
        nonlocal next_slot
        symbol, lo, hi = job
        name = f"{symbol}_{lo}_{hi}.json"
        # A longer cached page also supports an offline check of a shorter date range.
        candidates = [entry for entry in choices[symbol] if entry[0] <= lo and hi <= entry[1]]
        for _, _, existing in sorted(candidates, key=lambda entry: entry[2] != name):
            try:
                read_page(cache / existing, symbol)
                return [symbol, lo, hi, existing], None
            except (OSError, ValueError) as exc:
                cache_error = str(exc)
        if args.cache_only:
            return None, [symbol, lo, hi, cache_error if candidates else "No covering cached page"]
        for attempt in range(3):
            with slots_lock:
                slot = max(next_slot, time.monotonic())
                next_slot = slot + 0.2
            time.sleep(max(0.0, slot - time.monotonic()))
            try:
                req = Request(source_url(symbol, lo, hi), headers={"Referer": "https://www.sse.com.cn/",
                                                                 "User-Agent": "Mozilla/5.0"})
                with urlopen(req, timeout=12) as response:
                    raw = response.read().decode("utf-8")
                # Keep the exchange's original decimal text in the cache.
                data = json.loads(raw)
                validate_page(data, symbol)
                with atomic_output(cache / name, "utf-8") as out:
                    out.write(raw)
                return [symbol, lo, hi, name], None
            except (OSError, ValueError) as exc:
                reason = str(exc)
                if attempt < 2:
                    time.sleep(0.5 + attempt)
        return None, [symbol, lo, hi, reason]

    jobs = [(s, lo, hi) for s in stocks for lo, hi in ranges]
    pages, errors = [], []
    with ThreadPoolExecutor(max_workers=4) as workers:
        for page, error in workers.map(collect, jobs):
            if page:
                pages.append(page)
            else:
                errors.append(error)
    save_json(audit / "download_errors.json", errors)
    save_json(audit / "pages.json", {"start": start.isoformat(), "end": end.isoformat(),
              "funds": funds, "pcf_root": str(config["pcf_root"]), "dates": [d.isoformat() for d in dates],
              "ranges": ranges, "select": FIELDS, "pages": pages})
    print(f"{len(stocks)} stocks, {len(pages)}/{len(jobs)} exchange pages, {len(gaps)} missing PCFs."
          f" Results: {audit}")
    return 1 if errors or gaps else 0

if __name__ == "__main__":
    sys.exit(main())
