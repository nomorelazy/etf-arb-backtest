"""Pending money is a receivable/payable until its configured payment date."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


def trading_day_after(calendar, day, lag):
    """No calendar-day approximation or extrapolation beyond the supplied calendar."""
    index = calendar.index(day) + lag
    return calendar[index] if index < len(calendar) else None


@dataclass
class CashLeg:
    kind: str
    estimate: Decimal
    final: Decimal | None = None
    due: date | None = None
    known_on: date | None = None
    paid: bool = False

    @property
    def outstanding(self):
        return Decimal("0") if self.paid else (self.estimate if self.final is None else self.final)

    def observe_cash_component(self, pcf, opened_day, side, units, deposit, calendar, lag):
        # CashComponent is labelled by PreTradingDay, NOT by the PCF publication date.
        if self.kind != "cash_difference" or self.final is not None:
            return
        if pcf.pre_trading_day != opened_day or pcf.actual_cash_component is None:
            return
        amount = pcf.actual_cash_component * units
        if not amount.is_finite():
            raise ValueError("Non-finite actual cash component")
        self.final = deposit - amount if side.value == "CREATE" else deposit + amount
        self.known_on = pcf.effective_date
        self.due = trading_day_after(calendar, pcf.effective_date, lag)
