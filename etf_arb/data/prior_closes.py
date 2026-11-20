"""Sourced prior closes for execution and reporting."""

import csv
from datetime import date, datetime, time
import hashlib
from io import StringIO
from pathlib import Path
import re
from urllib.parse import urlsplit

from etf_arb.data.adjusted_references import AdjustedReferences
from etf_arb.data.minute_close import MinuteCloseBar
from decimal import Decimal


def _setting(config, key):
    return config.get(key) if isinstance(config, dict) else getattr(config, key, None)


def report_prior_path(config, override=None):
    supplied = override or _setting(config, "report_previous_closes_path")
    if supplied:
        return Path(supplied)
    root = _setting(config, "minute_root")
    if root is not None:
        path = Path(root) / "report_prior_closes.csv"
        if path.is_file():
            return path
    return None


def adjusted_references_match(data, config):
    for key in ("minute_root", "pcf_root"):
        root = _setting(config, key)
        if root and not Path(root).is_dir():
            return True
    return data.get("adjusted_references_sha256") == AdjustedReferences.from_config(config).sha256


def prior_closes_match(data, config):
    """A detached run may use its snapshot; an available changed feed invalidates it."""
    if not isinstance(data, dict):
        return False
    for key in ("minute_root", "pcf_root"):
        root = _setting(config, key)
        if root and not Path(root).is_dir():
            return True
    if data.get("adjusted_references_sha256") != AdjustedReferences.from_config(config).sha256:
        return False
    path = report_prior_path(config)
    if path and path.is_file():
        return data.get("prior_closes_sha256") == hashlib.sha256(path.read_bytes()).hexdigest()
    return not path and data.get("prior_closes_sha256") is None


class PriorCloses:
    """An optional, sourced daily file may supply a missing exact T-1 reference."""

    def __init__(self, prices, path):
        self.prices = prices
        self.rows = {}
        self.sha256 = None
        if path is None:
            return
        content = Path(path).read_bytes()
        self.sha256 = hashlib.sha256(content).hexdigest()
        reader = csv.DictReader(StringIO(content.decode("utf-8-sig")))
        if not {"previous_date", "symbol", "close", "source_url"} <= set(reader.fieldnames or ()):
            raise ValueError("Prior closes require previous_date,symbol,close,source_url")
        for row in reader:
            try:
                at = datetime.combine(date.fromisoformat(row["previous_date"].strip()), time(15))
                symbol = row["symbol"].strip().upper()
                price, url = Decimal(row["close"]), row["source_url"].strip()
                link = urlsplit(url)
                if (not re.fullmatch(r"\d{6}\.(SZ|SH)", symbol) or not price.is_finite() or price <= 0
                        or link.scheme not in {"https", "http"} or not link.netloc):
                    raise ValueError("Invalid prior close or provenance")
                if (symbol, at) in self.rows and self.rows[symbol, at] != (price, url):
                    raise ValueError("Conflicting prior close")
                self.rows[symbol, at] = price, url
            except (AttributeError, ArithmeticError, ValueError) as exc:
                raise ValueError(f"Invalid report prior close at {path}:{reader.line_num}") from exc

    def load(self, day, symbols, *, at=None):
        self.prices.load(day, symbols, at=at)

    def get(self, symbol, at, *, exact=False):
        bar = self.prices.get(symbol, at, exact=exact)
        if bar is not None or not exact:
            return bar
        # An invalid or conflicting quote is not repaired by silently choosing the feed.
        issue = getattr(self.prices, "issues", {}).get((symbol, at))
        record = self.rows.get((symbol, at))
        if record and issue in (None, "missing_close"):
            return MinuteCloseBar(at, symbol, record[0])
        return None
