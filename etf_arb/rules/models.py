"""Effective-dated ETF application and security trading rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from decimal import Decimal
from typing import Protocol

from etf_arb.domain import Side


def _normalize_code(value: str, field_name: str) -> str:
    code = str(value).strip()
    if not code:
        raise ValueError(f"{field_name} cannot be empty.")
    return code


def _decimal_value(value: Decimal | str | int | float, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a decimal number.") from error
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite.")
    return result


def _check_period(effective_from: date, effective_to: date | None) -> None:
    if type(effective_from) is not date:
        raise ValueError("effective_from must be a date.")
    if effective_to is not None and (
        type(effective_to) is not date or effective_to < effective_from
    ):
        raise ValueError("effective_to must be absent or on/after effective_from.")


@dataclass(frozen=True)
class ETFRule:
    """ETF creation/redemption settings for an inclusive date interval.

    Application units are expressed as multiples of the PCF creation unit.
    Fees are stored as absolute currency amounts per application unless the
    data source defines another unit; callers should normalize source values
    before constructing this record.
    """

    etf_code: str
    effective_from: date
    effective_to: date | None
    creation_enabled: bool
    redemption_enabled: bool
    creation_cutoff: time | None = None
    redemption_cutoff: time | None = None
    creation_fee: Decimal = Decimal("0")
    redemption_fee: Decimal = Decimal("0")
    min_creation_units: int = 1
    max_creation_units: int | None = None
    min_redemption_units: int = 1
    max_redemption_units: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "etf_code", _normalize_code(self.etf_code, "etf_code"))
        _check_period(self.effective_from, self.effective_to)
        for name in ("creation_fee", "redemption_fee"):
            value = _decimal_value(getattr(self, name), name)
            if value < 0:
                raise ValueError(f"{name} cannot be negative.")
            object.__setattr__(self, name, value)
        for side in ("creation", "redemption"):
            minimum = getattr(self, f"min_{side}_units")
            maximum = getattr(self, f"max_{side}_units")
            if minimum <= 0:
                raise ValueError(f"min_{side}_units must be positive.")
            if maximum is not None and maximum < minimum:
                raise ValueError(f"max_{side}_units cannot be below its minimum.")

    def applies_on(self, day: date) -> bool:
        return self.effective_from <= day and (
            self.effective_to is None or day <= self.effective_to
        )

    def allows(self, side: Side, units: int, at: time | None = None) -> bool:
        """Return whether the configured side, size, and optional cutoff allow an application."""
        if units <= 0:
            return False
        if side is Side.CREATE:
            enabled = self.creation_enabled
            cutoff = self.creation_cutoff
            minimum = self.min_creation_units
            maximum = self.max_creation_units
        elif side is Side.REDEEM:
            enabled = self.redemption_enabled
            cutoff = self.redemption_cutoff
            minimum = self.min_redemption_units
            maximum = self.max_redemption_units
        else:
            return False
        if (
            not enabled
            or units < minimum
            or (maximum is not None and units > maximum)
        ):
            return False
        return cutoff is None or at is None or at <= cutoff


@dataclass(frozen=True)
class SecurityTradingRule:
    """Effective-dated trading constraints and costs for a security."""

    security_code: str
    effective_from: date
    effective_to: date | None
    buy_enabled: bool
    sell_enabled: bool
    short_sell_enabled: bool | None = None
    can_sell_same_day: bool | None = None
    lot_size: int | None = None
    tick_size: Decimal | None = None
    limit_up_rate: Decimal | None = None
    limit_down_rate: Decimal | None = None
    commission_rate: Decimal | None = None
    minimum_commission: Decimal | None = None
    stamp_duty_rate: Decimal | None = None
    transfer_fee_rate: Decimal | None = None
    trading_start: time | None = None
    trading_end: time | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "security_code", _normalize_code(self.security_code, "security_code")
        )
        _check_period(self.effective_from, self.effective_to)
        if self.lot_size is not None and self.lot_size <= 0:
            raise ValueError("lot_size must be positive when provided.")
        decimal_fields = (
            "tick_size",
            "limit_up_rate",
            "limit_down_rate",
            "commission_rate",
            "minimum_commission",
            "stamp_duty_rate",
            "transfer_fee_rate",
        )
        for name in decimal_fields:
            raw = getattr(self, name)
            if raw is None:
                continue
            value = _decimal_value(raw, name)
            invalid = value <= 0 if name == "tick_size" else value < 0
            if invalid:
                raise ValueError(f"{name} must be {'positive' if name == 'tick_size' else 'non-negative'}.")
            object.__setattr__(self, name, value)
        if self.trading_start and self.trading_end and self.trading_start >= self.trading_end:
            raise ValueError("trading_start must be earlier than trading_end.")

    def applies_on(self, day: date) -> bool:
        return self.effective_from <= day and (
            self.effective_to is None or day <= self.effective_to
        )

    def can_buy(self) -> bool:
        return self.buy_enabled

    def can_sell(self) -> bool:
        return self.sell_enabled

    def is_trading_time(self, at: time) -> bool:
        return not (
            (self.trading_start is not None and at < self.trading_start)
            or (self.trading_end is not None and at > self.trading_end)
        )


class RuleRepository(Protocol):
    def get(self, etf_code: str, day: date) -> ETFRule:
        """Return the ETF rule effective on ``day``; never fall back to a current rule."""
        ...

    def get_security_rule(self, security_code: str, day: date) -> SecurityTradingRule:
        """Return the security trading rule effective on ``day``."""
        ...


__all__ = ["ETFRule", "RuleRepository", "SecurityTradingRule"]
