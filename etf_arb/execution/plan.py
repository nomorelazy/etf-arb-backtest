"""159338 SZSE cross-market routing; the 159900 row is an aggregate deposit."""

from dataclasses import dataclass
from decimal import Decimal

from etf_arb.data.snapshot import component_symbol
from etf_arb.domain import PCF, Side
from etf_arb.pricing.iopv import SubstitutionKind, _normalize_substitution_flag


ZERO = Decimal("0")


@dataclass(frozen=True)
class ArbitragePlan:
    physical: dict[str, int]
    manager: dict[str, int]
    manager_fixed: Decimal
    substitution_deposit: Decimal
    domestic_fixed: Decimal
    estimated_cash: Decimal
    etf_shares: int


def build_plan(pcf: PCF, side: Side, units: int) -> ArbitragePlan:
    if pcf.etf_code != "159338":
        raise ValueError("Full-flow execution currently supports only 159338")
    if type(units) is not int or units < 1:
        raise ValueError("Application units must be positive integers")
    for key, value in pcf.raw_fields:
        if key.lower() == "physicalcreationredemption" and value.upper() == "Y":
            raise ValueError("Non-SZ physical creation/redemption mode is not supported")
    if pcf.estimated_cash_component is None:
        raise ValueError("PCF has no estimated cash component")
    physical, manager, placeholders = {}, {}, []
    manager_fixed = domestic_fixed = ZERO
    amount_name = "creation_cash_substitute" if side is Side.CREATE else "redemption_cash_substitute"

    def amount(component):
        value = getattr(component, amount_name)
        if value is None or not value.is_finite() or value < 0:
            raise ValueError(f"Missing/invalid {amount_name}: {component.security_code}")
        return value * units

    has_sh = False
    for component in pcf.components:
        if component.is_cash_placeholder:
            placeholders.append(component)
            continue
        symbol = component_symbol(component)
        kind = _normalize_substitution_flag(component.substitution_flag)
        if component.market_code == "101":
            has_sh = True
            if kind is SubstitutionKind.FORBIDDEN:
                raise ValueError("SH physical substitution-forbidden component is unsupported")
            if kind is SubstitutionKind.MANDATORY:
                manager_fixed += amount(component)
                continue
            target = manager
        elif kind is SubstitutionKind.MANDATORY:
            domestic_fixed += amount(component)
            continue
        else:
            target = physical
        quantity = component.quantity * units
        if quantity != quantity.to_integral_value():
            raise ValueError(f"Non-integral component shares: {symbol}")
        if side is Side.CREATE and target is physical and quantity % 100:
            raise ValueError(f"Odd-lot physical creation requires an explicit substitution model: {symbol}")
        if symbol in target:
            raise ValueError(f"Duplicate PCF component: {symbol}")
        if quantity:
            target[symbol] = int(quantity)
    if len(placeholders) > 1 or (has_sh and len(placeholders) != 1):
        raise ValueError("Cross-market PCF must have exactly one aggregate cash placeholder")
    deposit = amount(placeholders[0]) if placeholders else ZERO
    return ArbitragePlan(physical, manager, manager_fixed, deposit, domestic_fixed,
                         pcf.estimated_cash_component * units, pcf.creation_unit * units)


def application_allowed(pcf, rule, side, units, at):
    enabled = pcf.creation_enabled if side is Side.CREATE else pcf.redemption_enabled
    if enabled is not True or not rule.allows(side, units, at):
        return False
    prefix = "creation" if side is Side.CREATE else "redemption"
    shares = pcf.creation_unit * units
    for name in (f"{prefix}_limit", f"{prefix}_limit_per_user",
                 f"net_{prefix}_limit", f"net_{prefix}_limit_per_user"):
        limit = getattr(pcf, name)
        if limit is not None and (not limit.is_finite() or limit < 0 or (limit > 0 and shares > limit)):
            return False
    return True  # A zero PCF limit means unlimited, not disabled.
