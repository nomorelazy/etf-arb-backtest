"""Route the basket by listing market and the day's cash-substitution flags."""

from dataclasses import dataclass, field
from decimal import Decimal

from etf_arb.data.snapshot import FULL_FLOW_ETFS, component_symbol, is_cross_market
from etf_arb.domain import PCF, Side
from etf_arb.pricing.iopv import SubstitutionKind, _normalize_substitution_flag, fixed_substitution_amount


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
    optional_creation_substitutions: dict[str, Decimal] = field(default_factory=dict)


def build_plan(pcf: PCF, side: Side, units: int, *, creation_cash_substitutions=None) -> ArbitragePlan:
    if pcf.etf_code not in FULL_FLOW_ETFS:
        raise ValueError(f"Unsupported full-flow ETF: {pcf.etf_code!r}")
    sh_listed = FULL_FLOW_ETFS[pcf.etf_code].endswith(".SH")
    if sh_listed and pcf.creation_redemption_mechanism not in (None, "1", "沪市成分证券实物对价"):
        raise ValueError(f"Unsupported SSE application mechanism: {pcf.creation_redemption_mechanism}")
    if type(units) is not int or units < 1:
        raise ValueError("Application units must be positive integers")
    for key, value in pcf.raw_fields:
        if key.lower() == "physicalcreationredemption" and value.upper() == "Y":
            raise ValueError("Non-SZ physical creation/redemption mode is not supported")
    if pcf.estimated_cash_component is None:
        raise ValueError("PCF has no estimated cash component")
    physical, manager, placeholders = {}, {}, []
    selected = dict(creation_cash_substitutions or {})
    optional_cash = {}
    if selected and side is not Side.CREATE:
        raise ValueError("Optional domestic substitution is only supported for creation")
    manager_fixed = domestic_fixed = ZERO
    cross_deposit = ZERO

    def amount(component):
        return fixed_substitution_amount(component, redemption=side is Side.REDEEM) * units

    has_cross_market = False
    for component in pcf.components:
        if component.is_cash_placeholder:
            placeholders.append(component)
            continue
        symbol = component_symbol(component)
        kind = _normalize_substitution_flag(component.substitution_flag)
        if component.quantity == 0 and kind is not SubstitutionKind.MANDATORY:
            continue
        if is_cross_market(pcf, symbol):
            has_cross_market = True
            if kind is SubstitutionKind.FORBIDDEN:
                raise ValueError(f"Cross-market physical delivery is unsupported: {symbol}")
            if kind is SubstitutionKind.MANDATORY:
                if sh_listed:
                    domestic_fixed += amount(component)
                else:
                    manager_fixed += amount(component)
                continue
            if sh_listed and side is Side.CREATE:
                reference_value = component.substitution_cash_amount
                rate = component.creation_premium_rate
                if (reference_value is None or not reference_value.is_finite() or reference_value <= 0
                        or rate is None or not rate.is_finite() or rate < 0):
                    raise ValueError(f"Missing/invalid cross-market creation deposit data: {symbol}")
                cross_deposit += reference_value * (1 + rate) * units
            target = manager
        elif kind is SubstitutionKind.MANDATORY:
            domestic_fixed += amount(component)
            continue
        elif symbol in selected:
            if kind is not SubstitutionKind.ALLOWED:
                raise ValueError(f"Cash substitution forbidden: {symbol}")
            value = Decimal(str(selected.pop(symbol)))
            if not value.is_finite() or value <= 0:
                raise ValueError(f"Missing/invalid optional substitution deposit: {symbol}")
            optional_cash[symbol] = value * units
            target = manager
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
    if sh_listed and placeholders:
        raise ValueError("Unexpected aggregate cash placeholder in an SSE physical basket")
    if len(placeholders) > 1 or (not sh_listed and has_cross_market and len(placeholders) != 1):
        raise ValueError("Cross-market PCF must have exactly one aggregate cash placeholder")
    if selected:
        raise ValueError(f"Invalid optional cash-substitution components: {','.join(selected)}")
    deposit = amount(placeholders[0]) if placeholders else cross_deposit
    deposit += sum(optional_cash.values(), ZERO)
    return ArbitragePlan(physical, manager, manager_fixed, deposit, domestic_fixed,
                         pcf.estimated_cash_component * units, pcf.creation_unit * units, optional_cash)


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
