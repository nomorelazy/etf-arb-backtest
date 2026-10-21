"""Pending money is a receivable/payable until its configured payment date."""

from dataclasses import dataclass
from datetime import date, datetime
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
    pricing_deadline: date | None = None
    attempt_scheduled_at: datetime | None = None
    notice_due_on: date | None = None
    latest_payment_on: date | None = None
    payment_at: datetime | None = None
    paid_at: datetime | None = None
    pricing_basis: str = ""
    payment_basis: str = "unknown"
    priced_at: datetime | None = None

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
        self.priced_at = datetime.combine(pcf.effective_date, datetime.min.time())
        if lag is not None and self.payment_at is None:
            self.due = trading_day_after(calendar, pcf.effective_date, lag)
            self.payment_basis = "explicit_research_lag"


def load_payment_dates(path):
    """JSON rows: trade_id, kind, paid_at; no calendar-generated receipts."""
    import json
    from pathlib import Path
    if path is None:
        return {}
    rows = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(rows, list):
        raise ValueError("Payment dates must be a JSON array")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"trade_id", "kind", "paid_at"}:
            raise ValueError("Each payment date needs trade_id, kind and paid_at")
        at = datetime.fromisoformat(row["paid_at"])
        key = row["trade_id"], row["kind"]
        if not isinstance(key[0], str) or not key[0] or key[1] not in {"cash_difference", "cash_substitution"}:
            raise ValueError("Invalid payment reference or kind")
        if at.tzinfo is not None or key in result:
            raise ValueError("Payment dates must be naive Beijing times and unique per leg")
        result[key] = at
    return result
