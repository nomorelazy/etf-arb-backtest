"""Explicit research assumptions for the first supported ETF execution model."""

from dataclasses import dataclass, field, replace
from decimal import Decimal
from etf_arb.costs import FeeRates
from etf_arb.rules.frequency import TradeFrequencyRule
from etf_arb.rules.trading_time import TradingTimeRule
from etf_arb.rules.cash_substitution import CashSubstitutionTiming


@dataclass(frozen=True)
class SimulationConfig:
    premium_threshold: Decimal | None = None
    quantity_units: int = 1
    max_trades_per_day: int | None = None
    min_trade_interval_minutes: int = 5
    blocked_trade_minutes: tuple[str, ...] = ("09:30",)
    trading_sessions: tuple[tuple[str, str], ...] = (("09:35", "11:29"), ("13:03", "14:57"))
    cash_substitution_pricing_days: int = 2
    cash_substitution_attempt_time: str = "14:30"
    # Optional, explicit research overrides only. None requires a supplied
    # payment timestamp and never equates a notice/deadline with received cash.
    cash_substitution_lag: int | None = None
    cash_difference_lag: int | None = None
    max_shares_per_minute: int | None = None
    security_rule_policy: str = "require"
    assume_full_basket_fill: bool = True
    fees: FeeRates = field(default_factory=FeeRates)
    market_split_execution: bool = True
    entry_at_signal_time: bool = True
    trade_direction: str = "both"
    creation_threshold: Decimal | None = None
    redemption_threshold: Decimal | None = None
    settle_estimated_cash_at_application: bool = False
    check_creation_feasibility: bool = True
    limit_loss_threshold: bool = False

    def __post_init__(self):
        if not isinstance(self.trade_direction, str) or self.trade_direction not in {"both", "creation", "redemption"}:
            raise ValueError("trade_direction must be both, creation or redemption")
        if isinstance(self.fees, dict):
            object.__setattr__(self, "fees", FeeRates.from_dict(self.fees))
        if not isinstance(self.fees, FeeRates):
            raise TypeError("fees must be FeeRates or a fee settings object")
        for name in ("premium_threshold", "creation_threshold", "redemption_threshold"):
            value = getattr(self, name)
            threshold = None if value is None else Decimal(str(value))
            if threshold is not None and (not threshold.is_finite() or not 0 < threshold < 1):
                raise ValueError(f"{name} must be a ratio between 0 and 1")
            object.__setattr__(self, name, threshold)
        TradeFrequencyRule(self.min_trade_interval_minutes, self.max_trades_per_day)
        trading_time = TradingTimeRule(self.blocked_trade_minutes, self.trading_sessions)
        object.__setattr__(self, "blocked_trade_minutes", trading_time.blocked_minutes)
        object.__setattr__(self, "trading_sessions", trading_time.sessions)
        cash_timing = CashSubstitutionTiming(self.cash_substitution_pricing_days, self.cash_substitution_attempt_time)
        object.__setattr__(self, "cash_substitution_attempt_time", cash_timing.attempt_time.strftime('%H:%M'))
        for name in ("quantity_units", "cash_substitution_pricing_days", "cash_substitution_lag", "cash_difference_lag"):
            value = getattr(self, name)
            if name.endswith("lag") and value is None:
                continue
            if type(value) is not int or value < (1 if name == "quantity_units" else 0):
                raise ValueError(f"Invalid {name}")
        if self.max_shares_per_minute is not None and (
            type(self.max_shares_per_minute) is not int or self.max_shares_per_minute < 1
        ):
            raise ValueError("max_shares_per_minute must be a positive integer")
        if self.security_rule_policy not in {"require", "assume_tradable"}:
            raise ValueError("security_rule_policy must be require or assume_tradable")
        if type(self.assume_full_basket_fill) is not bool:
            raise ValueError("assume_full_basket_fill must be a bool")
        if type(self.market_split_execution) is not bool:
            raise ValueError("market_split_execution must be a bool")
        if self.market_split_execution and not self.assume_full_basket_fill:
            raise ValueError("market_split_execution requires assume_full_basket_fill")
        if type(self.entry_at_signal_time) is not bool:
            raise ValueError("entry_at_signal_time must be a bool")
        if self.entry_at_signal_time and not self.assume_full_basket_fill:
            raise ValueError("entry_at_signal_time requires assume_full_basket_fill")
        if type(self.check_creation_feasibility) is not bool:
            raise ValueError("check_creation_feasibility must be a bool")
        if type(self.limit_loss_threshold) is not bool:
            raise ValueError("limit_loss_threshold must be a bool")
        if type(self.settle_estimated_cash_at_application) is not bool:
            raise ValueError("settle_estimated_cash_at_application must be a bool")

    def allows_side(self, side):
        return self.trade_direction == "both" or side.value == {
            "creation": "CREATE", "redemption": "REDEEM"}[self.trade_direction]

    @property
    def effective_creation_threshold(self):
        return self.creation_threshold if self.creation_threshold is not None else self.premium_threshold

    @property
    def effective_redemption_threshold(self):
        return self.redemption_threshold if self.redemption_threshold is not None else self.premium_threshold

    def with_threshold_overrides(self, *, premium_threshold=None, creation_threshold=None, redemption_threshold=None):
        """Apply the shared override first, then any direction-specific overrides."""
        changes = {}
        if premium_threshold is not None:
            changes.update(premium_threshold=premium_threshold, creation_threshold=None, redemption_threshold=None)
        if creation_threshold is not None:
            changes["creation_threshold"] = creation_threshold
        if redemption_threshold is not None:
            changes["redemption_threshold"] = redemption_threshold
        return replace(self, **changes) if changes else self
