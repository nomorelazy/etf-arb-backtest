"""Check whether the current creation basket can be bought or substituted."""

from dataclasses import dataclass, field
from decimal import Decimal

from etf_arb.data.snapshot import component_symbol, is_cross_market
from etf_arb.pricing.iopv import SubstitutionKind, _normalize_substitution_flag
from etf_arb.rules.tradability import security_trade_block


@dataclass
class CreationBasketCheck:
    blocked: dict[str, str] = field(default_factory=dict)
    cash_substitutions: dict[str, Decimal] = field(default_factory=dict)
    deposit_prices: dict[str, tuple[Decimal, str]] = field(default_factory=dict)
    substitution_reasons: dict[str, str] = field(default_factory=dict)
    data_errors: dict[str, str] = field(default_factory=dict)
    cash_substitution_ratio: Decimal | None = None
    max_cash_substitution_ratio: Decimal | None = None
    cash_substitution_reference_value: Decimal | None = None
    cash_substitution_reference_basis: str = ""
    cash_ratio_exceeded: bool = False

    @property
    def failure_reason(self):
        if self.cash_ratio_exceeded:
            return "creation_cash_ratio_exceeded"
        if self.blocked:
            return "unbuyable_and_no_cash_substitution"
        return "missing_cash_substitution_data" if self.data_errors else ""

    def signal_fields(self):
        return {name: getattr(self, name) for name in (
            "cash_substitution_ratio", "max_cash_substitution_ratio",
            "cash_substitution_reference_value", "cash_substitution_reference_basis")}


def check_creation_basket(pcf, prices, rules, now, policy, *, guard=None, reference_limits=None):
    """Use only currently known rules/quotes, never future fill availability.

    Use the effective prior close for 159338 domestic optional deposits.
    Amounts returned here are per creation unit; build_plan scales them once.
    """
    result = CreationBasketCheck()
    if reference_limits is None and guard is not None:
        reference_limits = guard.limits
    for component in pcf.components:
        if component.is_cash_placeholder or component.quantity == 0:
            continue
        symbol = component_symbol(component)
        try:
            kind = _normalize_substitution_flag(component.substitution_flag)
        except ValueError as error:
            result.data_errors[symbol] = str(error)
            continue
        if kind is SubstitutionKind.MANDATORY:
            continue
        # Cross-market securities already go through the manager.
        if is_cross_market(pcf, symbol) and kind is SubstitutionKind.ALLOWED:
            continue
        block = security_trade_block(rules, symbol, now, "BUY", policy)
        bar = prices.get(symbol, now, exact=True)
        block = block or ("missing_exact_bar" if bar is None else "")
        if not block and guard is not None:
            block = guard.buy_block(symbol, now)
            if block.startswith("unknown:"):
                result.data_errors[symbol] = block
                continue
        if not block:
            continue
        if kind is SubstitutionKind.FORBIDDEN:
            result.blocked[symbol] = block
            continue
        if pcf.etf_code == "159338" and symbol.endswith(".SZ"):
            price, basis = (reference_limits.creation_deposit_reference(pcf, symbol) if reference_limits is not None
                            else (None, "missing_creation_deposit_reference"))
            if price is None:
                result.data_errors[symbol] = basis
                continue
        else:
            if bar is None:
                result.data_errors[symbol] = "missing_substitution_deposit_close"
                continue
            price, basis = bar.close, "signal_minute_close"
        raw_rate = component.creation_premium_rate
        if raw_rate is None:
            result.data_errors[symbol] = "missing_creation_premium_rate"
            continue
        rate = Decimal(str(raw_rate))
        if not rate.is_finite() or rate < 0:
            result.data_errors[symbol] = "invalid_creation_premium_rate"
            continue
        result.deposit_prices[symbol] = price, basis
        result.cash_substitutions[symbol] = component.quantity * price * (1 + rate)
        result.substitution_reasons[symbol] = block
    if guard is not None:
        guard.check_cash_ratio(pcf, result)
    return result
