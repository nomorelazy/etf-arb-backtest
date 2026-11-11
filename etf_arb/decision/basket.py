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
    substitution_reasons: dict[str, str] = field(default_factory=dict)
    data_errors: dict[str, str] = field(default_factory=dict)


def check_creation_basket(pcf, prices, rules, now, policy):
    """Use only currently known rules/quotes, never future fill availability.

    Optional domestic substitution deposits use the user's research formula:
    current exact close * PCF shares * (1 + PCF creation premium).
    Amounts returned here are per creation unit; build_plan scales them once.
    """
    result = CreationBasketCheck()
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
        if not block:
            continue
        if kind is SubstitutionKind.FORBIDDEN:
            result.blocked[symbol] = block
            continue
        if bar is None:
            result.data_errors[symbol] = "missing_substitution_deposit_close"
            continue
        raw_rate = component.creation_premium_rate
        if raw_rate is None:
            result.data_errors[symbol] = "missing_creation_premium_rate"
            continue
        rate = Decimal(str(raw_rate))
        if not rate.is_finite() or rate < 0:
            result.data_errors[symbol] = "invalid_creation_premium_rate"
            continue
        result.cash_substitutions[symbol] = component.quantity * bar.close * (1 + rate)
        result.substitution_reasons[symbol] = block
    return result
