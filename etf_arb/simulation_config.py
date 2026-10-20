"""Explicit research assumptions for the first supported ETF execution model."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SimulationConfig:
    premium_threshold: Decimal = Decimal("0.04")
    quantity_units: int = 1
    max_trades_per_day: int = 1
    cash_substitution_lag: int = 3
    cash_difference_lag: int = 1
    max_shares_per_minute: int | None = None
    security_rule_policy: str = "require"

    def __post_init__(self):
        threshold = Decimal(str(self.premium_threshold))
        if not threshold.is_finite() or not 0 < threshold < 1:
            raise ValueError("premium_threshold must be between 0 and 1; 0.04 means 4%")
        object.__setattr__(self, "premium_threshold", threshold)
        for name in ("quantity_units", "max_trades_per_day", "cash_substitution_lag", "cash_difference_lag"):
            value = getattr(self, name)
            if type(value) is not int or value < (1 if name in {"quantity_units", "max_trades_per_day"} else 0):
                raise ValueError(f"Invalid {name}")
        if self.max_shares_per_minute is not None and (
            type(self.max_shares_per_minute) is not int or self.max_shares_per_minute < 1
        ):
            raise ValueError("max_shares_per_minute must be a positive integer")
        if self.security_rule_policy not in {"require", "assume_tradable"}:
            raise ValueError("security_rule_policy must be require or assume_tradable")
