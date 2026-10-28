"""User-configured trading windows, separate from exchange session definitions."""

from dataclasses import dataclass
from datetime import time, timedelta

@dataclass(frozen=True)
class TradingTimeRule:
    blocked_minutes: tuple[str, ...] = ("09:30",)
    sessions: tuple[tuple[str, str], ...] = (("09:35", "11:27"), ("13:00", "14:57"))

    def __post_init__(self):
        values = self.blocked_minutes
        if not isinstance(values, (tuple, list)):
            raise ValueError("blocked_trade_minutes must be an array of HH:MM strings")
        def parse(value):
            if not isinstance(value, str) or len(value) != 5:
                raise ValueError("Trading times must contain HH:MM strings")
            try:
                parsed = time.fromisoformat(value)
            except ValueError as error:
                raise ValueError("Invalid blocked trade minute") from error
            if parsed.tzinfo is not None or parsed.strftime("%H:%M") != value:
                raise ValueError("Invalid trade minute")
            return parsed.hour * 60 + parsed.minute
        for value in values:
            parse(value)
        object.__setattr__(self, "blocked_minutes", tuple(sorted(set(values))))
        if not isinstance(self.sessions, (tuple, list)) or not self.sessions:
            raise ValueError("trading_sessions must be a nonempty array of [start, end] pairs")
        sessions = []
        previous_end = -1
        for pair in self.sessions:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2:
                raise ValueError("Each trading session must have a start and end")
            start, end = map(parse, pair)
            if start > end or start <= previous_end:
                raise ValueError("Trading sessions must be increasing and non-overlapping")
            previous_end = end
            sessions.append(tuple(pair))
        object.__setattr__(self, "sessions", tuple(sessions))

    def is_blocked(self, at: time) -> bool:
        if not isinstance(at, time) or at.tzinfo is not None:
            raise ValueError("Trading times must be naive Beijing-local times")
        return at.strftime("%H:%M") in self.blocked_minutes

    def allows(self, at: time) -> bool:
        if self.is_blocked(at):
            return False
        minute = at.strftime("%H:%M")
        return any(start <= minute <= end for start, end in self.sessions)

    def has_later_minute_today(self, at: time) -> bool:
        """The next entry price must occur on a later allowed minute of T."""
        self.is_blocked(at)
        current = at.hour * 60 + at.minute
        for value in range(current + 1, 24 * 60):
            if self.allows(time(value // 60, value % 60)):
                return True
        return False

    def has_next_minute(self, now, clock_times) -> bool:
        """An exact t+1 runner minute, without bridging lunch or overnight."""
        target = now + timedelta(minutes=1)
        return (target.date() == now.date() and target.time() in clock_times
                and self.allows(target.time()))
