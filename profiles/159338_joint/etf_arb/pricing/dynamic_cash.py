"""Intraday estimate of one creation unit's cash component using the pre-open holdings sheet."""

from __future__ import annotations

import csv
import posixpath
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from etf_arb.data.snapshot import component_symbol
from etf_arb.domain import PCF
from etf_arb.pricing.errors import PricingDataError
from etf_arb.pricing.iopv import (
    SubstitutionKind, _component_quantity, _normalize_substitution_flag, fixed_substitution_amount,
)


ZERO = Decimal(0)
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _stock_symbol(value: str) -> str:
    raw = str(value).strip().upper()
    match = re.fullmatch(r"(\d{1,6})(?:\.(SH|SZ))?", raw)
    if not match:
        raise ValueError(f"Invalid holdings security code: {value!r}")
    code = match[1].zfill(6)
    exchange = match[2]
    if not exchange:
        if code.startswith(("00", "30")):
            exchange = "SZ"
        elif code.startswith(("60", "68")):
            exchange = "SH"
        else:
            raise ValueError(f"Holdings security needs an exchange suffix: {value!r}")
    return f"{code}.{exchange}"


def _canonical(symbol: str, day: date) -> str:
    # The vendor uses 302132 for historical bars of the same stock formerly named 300114.
    if symbol == "300114.SZ" and day < date(2025, 2, 17):
        return "302132.SZ"
    return symbol


def _price_symbols(symbol: str, day: date) -> tuple[str, ...]:
    if symbol == "302132.SZ" and day < date(2025, 2, 17):
        return ("300114.SZ", "302132.SZ")
    return (symbol,)


def _sheet_day(raw: str, epoch: date) -> date:
    if re.fullmatch(r"\d{8}", raw):
        return datetime.strptime(raw, "%Y%m%d").date()
    if re.fullmatch(r"\d{1,5}(?:\.0+)?", raw):
        return epoch + timedelta(days=int(Decimal(raw)))
    return date.fromisoformat(raw)


def _shared_text(element) -> str:
    return "".join(node.text or "" for node in element.iterfind(".//{*}t"))


def _holdings_from_xlsx(path: Path, etf_code: str) -> dict:
    """Stream the selected ETF worksheet, keeping only the explicit 零股数量 column."""
    holdings = {}
    with ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        properties = workbook.find("{*}workbookPr")
        uses_1904 = properties is not None and properties.get("date1904") in {"1", "true"}
        epoch = date(1904, 1, 1) if uses_1904 else date(1899, 12, 30)
        sheet = next((el for el in workbook.findall(".//{*}sheet") if el.get("name") == etf_code), None)
        if sheet is None:
            raise ValueError(f"No ETF worksheet {etf_code} in {path}")
        relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        target = next(el.attrib["Target"] for el in relationships if el.get("Id") == sheet.get(_REL_ID))
        member = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = [_shared_text(el) for el in ET.fromstring(archive.read("xl/sharedStrings.xml"))]
        columns = {}
        with archive.open(member) as stream:
            for _, row in ET.iterparse(stream, events=("end",)):
                if not row.tag.endswith("}row"):
                    continue
                values = {}
                for cell in row:
                    if not cell.tag.endswith("}c"):
                        continue
                    column = cell.attrib["r"].rstrip("0123456789")
                    value = cell.findtext("{*}v")
                    if cell.get("t") == "inlineStr":
                        value = _shared_text(cell)
                    elif cell.get("t") == "s" and value is not None:
                        value = strings[int(value)]
                    if value is not None and value.strip():
                        values[column] = value.strip()
                line = row.get("r")
                row.clear()
                if not values:
                    continue
                if not columns:
                    columns = {value.lower(): col for col, value in values.items()}
                    if not {"股票代码", "零股数量", "日期"} <= columns.keys():
                        raise ValueError(f"{path}/{etf_code}: expected 股票代码, 零股数量, 日期")
                    continue
                try:
                    if "etf代码" in columns and values[columns["etf代码"]].split(".")[0] != etf_code:
                        raise ValueError("ETF code does not match worksheet")
                    day = _sheet_day(values[columns["日期"]], epoch)
                    raw_symbol = values[columns["股票代码"]]
                    if raw_symbol.split(".")[0] == "159900":
                        continue
                    symbol = _canonical(_stock_symbol(raw_symbol), day)
                    quantity = Decimal(values[columns["零股数量"]])
                    if not quantity.is_finite() or quantity < 0:
                        raise ValueError("holding quantity must be finite and nonnegative")
                    snapshot = holdings.setdefault(day, {})
                    if symbol in snapshot:
                        raise ValueError(f"duplicate holding {day} {symbol}")
                    snapshot[symbol] = quantity
                except (KeyError, ValueError, InvalidOperation) as error:
                    raise ValueError(f"Invalid holdings at {path}/{etf_code} row {line}: {error}") from error
    if not holdings:
        raise ValueError(f"Empty holdings worksheet {etf_code} in {path}")
    return holdings


def _adjusted_references(path: Path) -> dict:
    """Only the T-day pre-open preClose field is a valuation input; never read T's close."""
    result = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"time", "thscode", "preClose"} <= set(reader.fieldnames or ()):
            raise ValueError(f"{path}: expected time, thscode, preClose")
        for row in reader:
            raw = row["preClose"].strip()
            if not raw or raw.lower() in {"nan", "na", "null", "none", "--"}:
                continue
            day = date.fromisoformat(row["time"][:10])
            symbol = _canonical(_stock_symbol(row["thscode"]), day)
            price = Decimal(raw)
            if not price.is_finite() or price <= 0:
                continue
            prices = result.setdefault(day, {})
            if symbol in prices and price != prices[symbol]:
                raise ValueError(f"Conflicting adjusted references: {day} {symbol}")
            prices[symbol] = price
    return result


@dataclass(frozen=True)
class DynamicCashValuation:
    security_value: Decimal
    mandatory_cash: Decimal
    pcf_estimated_cash: Decimal
    cash_adjustment: Decimal
    creation_unit: int
    price_count: int
    # All accepted prices are exact-minute observations.
    stale_quote_count: int = 0

    @property
    def estimated_cash(self):
        return self.pcf_estimated_cash + self.cash_adjustment

    @property
    def numerator(self):
        return self.security_value + self.mandatory_cash + self.estimated_cash

    @property
    def iopv(self):
        return self.numerator / self.creation_unit


@dataclass(frozen=True)
class _DayInputs:
    # (historical quote symbols of the same security, floating PCF shares, effective exposure)
    rows: tuple
    mandatory_cash: Decimal
    reference_exposure_value: Decimal


class DynamicCashModel:
    def __init__(self, etf_code: str, holdings: dict, reference_prices: dict):
        self.etf_code = etf_code
        self.holdings = holdings
        self.reference_prices = reference_prices
        self._pcf = None
        self._inputs = None
        self._error = None

    @classmethod
    def from_files(cls, holdings_path, reference_prices_path, etf_code):
        return cls(etf_code, _holdings_from_xlsx(Path(holdings_path), etf_code),
                   _adjusted_references(Path(reference_prices_path)))

    def required_symbols(self, pcf: PCF) -> set[str]:
        """Extra quote subscriptions for positive stock holdings, including fixed-cash stocks."""
        if pcf.etf_code != self.etf_code:
            raise ValueError(f"Holdings for {self.etf_code} cannot value {pcf.etf_code}")
        day = pcf.effective_date
        symbols = {s for s, q in self.holdings.get(day, {}).items() if q}
        # An old PCF code may be represented solely by the new alias in market data.
        if any(c.security_code == "300114" for c in pcf.components):
            symbols.add(_canonical("300114.SZ", day))
        return {name for s in symbols for name in _price_symbols(s, day)}

    def _prepare(self, pcf: PCF) -> _DayInputs:
        if pcf.etf_code != self.etf_code:
            raise ValueError(f"Holdings for {self.etf_code} cannot value {pcf.etf_code}")
        day = pcf.effective_date
        holdings = self.holdings.get(day)
        if not holdings:
            raise PricingDataError(f"missing_odd_lot_holdings: {self.etf_code} {day}")
        if pcf.estimated_cash_component is None or not pcf.estimated_cash_component.is_finite():
            raise PricingDataError(f"missing_pcf_estimated_cash: {day}")
        basket = {}
        fixed = ZERO
        for component in pcf.components:
            if component.is_cash_placeholder or component.security_code == "159900":
                continue
            symbol = _canonical(component_symbol(component), day)
            if symbol in basket:
                raise PricingDataError(f"duplicate_pcf_stock: {symbol}")
            kind = _normalize_substitution_flag(component.substitution_flag)
            if kind is SubstitutionKind.MANDATORY:
                fixed += fixed_substitution_amount(component)
                basket[symbol] = ZERO
            else:
                basket[symbol] = _component_quantity(component)
        refs = self.reference_prices.get(day, {})
        rows, missing = [], []
        reference_value = ZERO
        for symbol in sorted(basket.keys() | holdings.keys()):
            if symbol == "159900.SZ":
                continue
            basket_quantity = basket.get(symbol, ZERO)
            # A supplied, complete ETF/day sheet is a snapshot; a stock absent from it has zero weight.
            exposure = holdings.get(symbol, ZERO) - basket_quantity
            if exposure:
                reference = refs.get(symbol)
                if reference is None:
                    missing.append(symbol)
                    continue
                reference_value += exposure * reference
            if exposure or basket_quantity:
                rows.append((_price_symbols(symbol, day), basket_quantity, exposure))
        if missing:
            raise PricingDataError("missing_adjusted_preclose: " + ",".join(missing))
        return _DayInputs(tuple(rows), fixed, reference_value)

    def value(self, pcf: PCF, now: datetime, prices) -> DynamicCashValuation:
        if now.tzinfo is not None or now.date() != pcf.effective_date:
            raise ValueError("Dynamic valuation requires the PCF date and a local, timezone-naive minute")
        if self._pcf is not pcf:
            self._inputs, self._error = None, None
            try:
                self._inputs = self._prepare(pcf)
            except PricingDataError as error:
                self._error = str(error)
            self._pcf = pcf
        if self._error is not None:
            raise PricingDataError(self._error)
        inputs = self._inputs
        adjustment, security_value = -inputs.reference_exposure_value, ZERO
        missing = []
        for aliases, basket_quantity, exposure in inputs.rows:
            for symbol in aliases:
                bar = prices.get(symbol, now, exact=True)
                if bar is not None and bar.time == now and bar.close.is_finite() and bar.close > 0:
                    break
            else:
                missing.append(aliases[0])
                continue
            security_value += basket_quantity * bar.close
            adjustment += exposure * bar.close
        if missing:
            raise PricingDataError("missing_exact_valuation_price: " + ",".join(missing))
        return DynamicCashValuation(security_value, inputs.mandatory_cash, pcf.estimated_cash_component,
                                    adjustment, pcf.creation_unit, len(inputs.rows))
