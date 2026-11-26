"""Dated, sourced ex-rights reference prices for the effective trading day."""
import csv
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


ADJUSTED_REFERENCE_SOURCE = "adjusted_reference_price"


class AdjustedReferences:
    """The default feed is sourced; old explicit three-column files support the cash cap."""

    @classmethod
    def from_config(cls, config):
        setting = config.get if isinstance(config, dict) else lambda k: getattr(config, k, None)
        return cls(setting("minute_root"), setting("creation_reference_prices_path"),
                   verified_path=setting("adjusted_reference_prices_path"))

    def __init__(self, minute_root=None, explicit_path=None, *, verified_path=None):
        self.prices = {}
        self.limits = {}
        self.urls = {}
        shared = Path(minute_root) / "adjusted_reference_prices.csv" if minute_root else None
        explicit = Path(explicit_path) if explicit_path else None
        if shared and shared.is_file():
            self._read(shared, require_urls=True)
        if explicit and (shared is None or explicit.resolve() != shared.resolve() or not shared.is_file()):
            self._read(explicit, require_urls=False)
        if verified_path:
            self._read(Path(verified_path), require_urls=True)
        # Only values used by limit classification affect its historical report snapshots.
        feed = [[day.isoformat(), symbol, str(price), self.urls[day, symbol]]
                for (day, symbol), price in sorted(self.limits.items())]
        encoded = json.dumps(feed, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.sha256 = hashlib.sha256(encoded).hexdigest() if feed else None

    def _read(self, path, *, require_urls):
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            columns = set(reader.fieldnames or ())
            required = {"date", "symbol", "reference_price"}
            if not required <= columns or (require_urls and "source_url" not in columns):
                raise ValueError(f"Invalid adjusted reference columns at {path}")
            seen = set()
            for row in reader:
                try:
                    day = date.fromisoformat(row["date"].strip())
                    symbol = row["symbol"].strip().upper()
                    price = Decimal(row["reference_price"].strip())
                    key = day, symbol
                    if (not re.fullmatch(r"\d{6}\.(SZ|SH)", symbol) or not price.is_finite()
                            or price <= 0 or key in seen):
                        raise ValueError("Invalid or duplicate reference price")
                    seen.add(key)
                    url = (row.get("source_url") or "").strip()
                    if require_urls or "source_url" in columns:
                        source = urlsplit(url)
                        if source.scheme not in {"https","http"} or not source.netloc:
                            raise ValueError("Invalid reference source URL")
                        self.limits[key] = price
                        self.urls[key] = url
                    self.prices[key] = price
                except (AttributeError, InvalidOperation, ValueError) as error:
                    raise ValueError(f"Invalid adjusted reference at {path}:{reader.line_num}: {error}") from error
