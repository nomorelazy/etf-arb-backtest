"""Batch-load a trading day; consumers can only request current/past bars."""

from bisect import bisect_right
from datetime import datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow.dataset as ds

from etf_arb.data.minute_close import MinuteCloseBar, MinuteCloseConflictError, MinuteCloseDataError


FULL_FLOW_ETFS = {
    "159338": "159338.SZ",
    "159516": "159516.SZ",
    "512760": "512760.SH",
    "512880": "512880.SH",
    "515880": "515880.SH",
}


def component_symbol(component):
    suffix = {
        "101": "SH", "上交所": "SH", "上海证券交易所": "SH",
        "102": "SZ", "深交所": "SZ", "深圳证券交易所": "SZ",
    }.get(component.market_code)
    if component.market_code is None:
        if component.security_code.startswith(("60", "68")):
            suffix = "SH"
        elif component.security_code.startswith(("00", "30")):
            suffix = "SZ"
    if suffix is None:
        raise ValueError(f"Missing/unsupported PCF market: {component.security_code}/{component.market_code}")
    return f"{component.security_code}.{suffix}"


def is_cross_market(pcf, symbol):
    return symbol.rsplit(".", 1)[-1] != FULL_FLOW_ETFS[pcf.etf_code].rsplit(".", 1)[-1]


class DailyCloseCache:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.day = None
        self.bars = {}
        self.times = {}

    def load(self, day, symbols):
        files = sorted((self.root / f"{day:%Y-%m}").glob("*.parquet"))
        self.day, self.bars, self.times = day, {}, {}
        if not files:
            return
        start = datetime.combine(day, time.min)
        table = ds.dataset([str(p) for p in files], format="parquet").to_table(
            columns=["time", "thscode", "close"],
            filter=(ds.field("time") >= start) & (ds.field("time") < start + timedelta(days=1))
            & ds.field("thscode").isin(sorted(symbols)),
        )
        rows = {}
        for ts, code, raw in zip(*(table[n].to_pylist() for n in ("time", "thscode", "close"))):
            if raw is None:
                continue
            price = Decimal(str(raw))
            if not price.is_finite() or price <= 0:
                raise MinuteCloseDataError(f"Invalid close: {code} {ts} {raw}")
            key = (code, ts)
            if key in rows and rows[key] != price:
                raise MinuteCloseConflictError(f"Conflicting close: {code} {ts}")
            rows[key] = price
        for (code, ts), price in sorted(rows.items()):
            self.bars.setdefault(code, []).append(MinuteCloseBar(ts, code, price))
        self.times = {code: [bar.time for bar in bars] for code, bars in self.bars.items()}

    def get(self, symbol, now, *, exact=False):
        if now.date() != self.day:
            raise ValueError("Cache queries must be on the currently loaded trading day")
        index = bisect_right(self.times.get(symbol, ()), now) - 1
        if index < 0:
            return None
        bar = self.bars[symbol][index]
        return None if exact and bar.time != now else bar
