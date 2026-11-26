"""Point-in-time close-touch filter for the PCF stock basket."""

import csv
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from etf_arb.data.adjusted_references import ADJUSTED_REFERENCE_SOURCE, AdjustedReferences
from etf_arb.data.snapshot import DailyCloseCache, component_symbol
from etf_arb.rules.a_share import AShareBoard, a_share_price_limit_rate
from etf_arb.rules.creation_deposit import creation_deposit_references
from etf_arb.rules.errors import RuleNotFoundError
from etf_arb.rules.limit_reference import load_limit_references, opening_proxy_is_future


TOLERANCE = Decimal("0.000001")
STOCK_TICK = Decimal("0.01")


def load_daily_bounds(path):
    bounds = {}
    if path is None:
        return bounds
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"date", "symbol", "upper_limit", "lower_limit"}
        if not required.issubset(reader.fieldnames or ()):
            raise ValueError("Price limit CSV requires date,symbol,upper_limit,lower_limit")
        for row in reader:
            try:
                day = date.fromisoformat(row["date"].strip())
                symbol = row["symbol"].strip().upper()
                if not symbol.endswith((".SH", ".SZ")):
                    raise ValueError("symbol must include its exchange suffix")
                flag = (row.get("no_limit") or "").strip().lower()
                if flag in {"1", "true", "yes"}:
                    value = (None, None)
                elif flag in {"", "0", "false", "no"}:
                    upper, lower = Decimal(row["upper_limit"]), Decimal(row["lower_limit"])
                    if not (upper.is_finite() and lower.is_finite() and upper > lower > 0):
                        raise ValueError("upper and lower limits must be finite, positive and increasing")
                    value = (upper, lower)
                else:
                    raise ValueError("invalid no_limit")
                if (day, symbol) in bounds and bounds[day, symbol] != value:
                    raise ValueError("conflicting limits for this date and symbol")
                bounds[day, symbol] = value
            except (AttributeError, InvalidOperation, TypeError, ValueError) as error:
                raise ValueError(f"Invalid price limit CSV row {reader.line_num}: {error}") from error
    return bounds


def estimate_bounds(reference, up_rate, down_rate, tick):
    upper = (reference * (1 + up_rate) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick
    lower = (reference * (1 - down_rate) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick
    return max(reference + tick, upper), max(tick, min(reference - tick, lower))


class PriceLimitFilter:
    def __init__(self, prices, minute_root, bounds_path=None, *, previous_closes=None, adjusted_references=None):
        self.prices = prices
        self.adjusted = adjusted_references if adjusted_references is not None else AdjustedReferences(minute_root)
        self.previous = previous_closes if previous_closes is not None else DailyCloseCache(minute_root)
        self.opening = DailyCloseCache(minute_root)
        self.provided = load_daily_bounds(bounds_path)
        self.bounds = {}
        self.deposit_pcf = None
        self.last_check = None
        self.last_result = {}

    def creation_deposit_reference(self, pcf, symbol):
        if self.deposit_pcf is not pcf:
            self.deposit_references = creation_deposit_references(pcf, self.previous, self.adjusted)
            self.deposit_pcf = pcf
        return self.deposit_references.get(symbol, (None, "missing_creation_deposit_reference"))

    def on_day_open(self, pcf, rules, *, check_basket=True, held_components=()):
        day = pcf.effective_date
        components = {
            component_symbol(component): component for component in pcf.components
            if check_basket and not component.is_cash_placeholder and component.quantity > 0
        }
        self.basket_symbols = tuple(components)
        for component in held_components:
            components.setdefault(component_symbol(component), component)
        dated_rules = {}
        for symbol, component in components.items():
            for key in (symbol, component.security_code):
                try:
                    dated_rules[symbol] = rules.get_security_rule(key, day)
                    break
                except RuleNotFoundError:
                    continue
        # Halts go through the basket's PCF substitution and tradability checks.
        # Strict whole-basket filtering still applies to other constituents.
        self.basket_symbols = tuple(symbol for symbol in self.basket_symbols
                                    if symbol not in dated_rules or not dated_rules[symbol].is_suspended)
        self.bounds = {symbol: (self.provided[day, symbol], "provided")
                       for symbol in components if (day, symbol) in self.provided}
        # A price carried across a halt cannot touch a tradable limit.
        suspensions = getattr(rules, "suspensions", None)
        if suspensions is not None:
            for symbol in components:
                if suspensions.on(symbol, day):
                    self.bounds[symbol] = ((None, None), "stock_suspended")
        estimated_symbols = components.keys() - self.bounds.keys()
        references = load_limit_references(pcf, estimated_symbols, self.previous, self.opening, self.adjusted)
        for symbol in sorted(estimated_symbols):
            reference, source = references[symbol]
            if reference is None:
                self.bounds[symbol] = (None, "missing_previous_close")
                continue
            component = components[symbol]
            code = component.security_code
            if code.startswith(("688", "689")):
                board = AShareBoard.STAR
            elif code.startswith(("300", "301")):
                board = AShareBoard.CHINEXT
            elif code.startswith(("000", "001", "002", "003", "600", "601", "603", "605")):
                board = AShareBoard.MAIN
            else:
                self.bounds[symbol] = (None, "unknown_board")
                continue
            rule = dated_rules.get(symbol)
            name = (component.security_name or "").upper().lstrip()
            if name.startswith(("N", "C")) or (name.startswith(("XD", "XR", "DR"))
                                                and source != ADJUSTED_REFERENCE_SOURCE):
                self.bounds[symbol] = (None, "needs_official_bounds")
                continue
            if name.startswith(("XD", "XR", "DR")):
                name = name[2:].lstrip()
            rate = a_share_price_limit_rate(
                board, day, is_st=rule.is_st if rule else name.startswith(("ST", "*ST", "SST", "S*ST")),
                is_suspended=bool(rule and rule.is_suspended),
                is_delisting_period=bool(rule and rule.is_delisting_period),
                delisting_trading_day=rule.delisting_trading_day if rule else None,
            )
            up = rule.limit_up_rate if rule and rule.limit_up_rate is not None else rate
            down = rule.limit_down_rate if rule and rule.limit_down_rate is not None else rate
            if up is None or down is None or up <= 0 or down <= 0:
                self.bounds[symbol] = (None, "needs_official_bounds")
                continue
            tick = rule.tick_size if rule and rule.tick_size is not None else STOCK_TICK
            self.bounds[symbol] = (estimate_bounds(reference.close, up, down, tick), source)
        self.last_check = None

    def blocked(self, now):
        if now == self.last_check:
            return self.last_result
        result = {symbol: reason for symbol in self.basket_symbols
                  if (reason := self.status(symbol, now)) is not None}
        self.last_check, self.last_result = now, result
        return result

    def status(self, symbol, now):
        bounds, source = self.bounds.get(symbol, (None, "missing_bounds"))
        if opening_proxy_is_future(source, now):
            return "unknown:opening_proxy_not_yet_available"
        if bounds is None:
            return f"unknown:{source}"
        upper, lower = bounds
        if upper is None:
            return None
        quote = self.prices.get(symbol, now, exact=True)
        if quote is None:
            return "unknown:missing_exact_close"
        if quote.close > upper + TOLERANCE or quote.close < lower - TOLERANCE:
            return f"unknown:outside_{source}_bounds"
        if abs(quote.close - upper) <= TOLERANCE:
            return f"limit_up:{source}"
        if abs(quote.close - lower) <= TOLERANCE:
            return f"limit_down:{source}"
        return None


def price_limit_reason(blocked):
    return "price_limit_touched" if any(value.startswith("limit_") for value in blocked.values()) else "price_limit_unknown"
