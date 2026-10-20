"""Rolling elapsed-time limits on arbitrage starts, independent of order legs."""

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class TradeFrequencyRule:
    min_interval_minutes: int = 60
    max_trades_per_day: int | None = None

    def __post_init__(self):
        if type(self.min_interval_minutes) is not int or self.min_interval_minutes < 1:
            raise ValueError("min_interval_minutes must be a positive integer")
        cap = self.max_trades_per_day
        if cap is not None and (type(cap) is not int or cap < 1):
            raise ValueError("max_trades_per_day must be null or a positive integer")

    def allows(self, at: datetime, *, last_started_at: datetime | None, started_today: int) -> bool:
        for ts in (at, last_started_at):
            if ts is not None and (not isinstance(ts, datetime) or ts.tzinfo is not None):
                raise ValueError("Trade start timestamps must be naive Beijing datetimes")
        if last_started_at is not None and at - last_started_at < timedelta(minutes=self.min_interval_minutes):
            return False
        return self.max_trades_per_day is None or started_today < self.max_trades_per_day
