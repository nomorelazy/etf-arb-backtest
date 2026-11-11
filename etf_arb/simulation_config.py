"""Explicit research assumptions for the first supported ETF execution model."""

from dataclasses import dataclass, field
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
    min_trade_interval_minutes: int = 60
    blocked_trade_minutes: tuple[str, ...] = ("09:30",)
    trading_sessions: tuple[tuple[str, str], ...] = (("09:35", "11:27"), ("13:00", "14:57"))
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
    settle_estimated_cash_at_application: bool = False

    def __post_init__(self):
        if isinstance(self.fees, dict):
            object.__setattr__(self, "fees", FeeRates.from_dict(self.fees))
        if not isinstance(self.fees, FeeRates):
            raise TypeError("fees must be FeeRates or a fee settings object")
        threshold = None if self.premium_threshold is None else Decimal(str(self.premium_threshold))
        if threshold is not None and (not threshold.is_finite() or not 0 < threshold < 1):
            raise ValueError("premium_threshold must be a ratio between 0 and 1; set it explicitly for each run")
        object.__setattr__(self, "premium_threshold", threshold)
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
        if type(self.settle_estimated_cash_at_application) is not bool:
            raise ValueError("settle_estimated_cash_at_application must be a bool")
