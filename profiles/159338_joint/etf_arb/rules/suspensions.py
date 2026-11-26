"""Effective-dated stock suspensions from an attributable historical feed."""
import csv
from dataclasses import dataclass, replace
from datetime import date
import hashlib
from io import StringIO
from pathlib import Path
import re
from urllib.parse import urlsplit

from etf_arb.rules.errors import RuleNotFoundError
from etf_arb.rules.models import SecurityTradingRule


@dataclass(frozen=True)
class Suspension:
    symbol: str
    start_date: date
    end_date: date
    source_url: str


class StockSuspensions:
    @classmethod
    def from_config(cls, config):
        setting = config.get if isinstance(config, dict) else lambda key: getattr(config, key, None)
        supplied = setting("stock_suspensions_path")
        if supplied:
            return cls(Path(supplied))
        root = setting("minute_root")
        shared = Path(root) / "stock_suspensions.csv" if root else None
        return cls(shared if shared and shared.is_file() else None)

    def __init__(self, path=None):
        self.periods = {}
        self.sha256 = None
        if path is None:
            return
        path = Path(path)
        content = path.read_bytes()
        self.sha256 = hashlib.sha256(content).hexdigest()
        reader = csv.DictReader(StringIO(content.decode("utf-8-sig")))
        if not {"symbol", "start_date", "end_date", "source_url"} <= set(reader.fieldnames or ()):
            raise ValueError(f"Invalid suspension columns at {path}")
        for raw in reader:
            try:
                symbol = raw["symbol"].strip().upper()
                start = date.fromisoformat(raw["start_date"].strip())
                end = date.fromisoformat(raw["end_date"].strip())
                url = raw["source_url"].strip()
                link = urlsplit(url)
                if not re.fullmatch(r"[0-9]{6}\.(SH|SZ)", symbol) or start > end:
                    raise ValueError("Invalid symbol or date range")
                if link.scheme not in {"https", "http"} or not link.netloc:
                    raise ValueError("Invalid suspension source URL")
                periods = self.periods.setdefault(symbol, [])
                if any(start <= prior.end_date and end >= prior.start_date for prior in periods):
                    raise ValueError("Overlapping suspension intervals")
                periods.append(Suspension(symbol, start, end, url))
            except (AttributeError, TypeError, ValueError) as error:
                raise ValueError(f"Invalid suspension at {path}:{reader.line_num}: {error}") from error

    def on(self, symbol, day):
        return next((period for period in self.periods.get(symbol, ())
                     if period.start_date <= day <= period.end_date), None)


class SuspensionRules:
    def __init__(self, base, suspensions):
        self.base = base
        self.suspensions = suspensions

    def __getattr__(self, name):
        # Keep the base's dated-history gap policy; a sparse outage feed does
        # not claim that a stock is missing a rule after resumption.
        return getattr(self.base, name)

    def get_security_rule(self, security_code, day):
        period = self.suspensions.on(security_code, day)
        if period is None:
            return self.base.get_security_rule(security_code, day)
        for code in (security_code, security_code.split(".")[0]):
            try:
                rule = self.base.get_security_rule(code, day)
            except RuleNotFoundError:
                continue
            return replace(rule, is_suspended=True, delisting_trading_day=None)
        return SecurityTradingRule(security_code, period.start_date, period.end_date,
                                   buy_enabled=False, sell_enabled=False, is_suspended=True)


def with_suspensions(config, rules):
    if isinstance(rules, SuspensionRules):
        return rules
    feed = StockSuspensions.from_config(config)
    return SuspensionRules(rules, feed) if feed.sha256 is not None else rules
