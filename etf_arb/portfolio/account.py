"""Cash/holdings projection. Cash flows, rather than signal edges, determine PnL."""

from decimal import Decimal
from etf_arb.costs import FeeBreakdown

from etf_arb.portfolio.ledger import LedgerEvent, LedgerEventType


ZERO = Decimal("0")


class CashAccount:
    def __init__(self, starting_cash, ledger):
        self.starting_cash = self.cash = starting_cash
        self.ledger = ledger
        self.positions = {}  # (trade_id, symbol) -> shares
        self.trade_cash = {}
        self.rows = []

    def post(self, ts, trade_id, kind, *, cash=ZERO, symbol=None, quantity=0):
        cash = Decimal(cash)
        if not cash.is_finite() or self.cash + cash < 0:
            raise ValueError("Invalid cash flow or insufficient cash; borrowing is disabled")
        old = self.positions.get((trade_id, symbol), 0) if symbol else 0
        if old + quantity < 0:
            raise ValueError("Insufficient shares; short selling is disabled")
        if quantity and not symbol:
            raise ValueError("Security movements require a symbol")
        self.ledger.record(LedgerEvent(ts, kind, trade_id, cash, symbol, quantity))
        self.cash += cash
        self.trade_cash[trade_id] = self.trade_cash.get(trade_id, ZERO) + cash
        if symbol:
            self.positions[trade_id, symbol] = old + quantity
        self.rows.append(dict(ts=ts, trade_id=trade_id, event=kind.value, cash_delta=cash,
                              symbol=symbol, quantity_delta=quantity, cash_balance=self.cash))

    def fill(self, ts, trade_id, symbol, side, quantity, price, *, fees=None):
        if type(quantity) is not int or quantity <= 0 or not price.is_finite() or price <= 0:
            raise ValueError("Invalid fill quantity/price")
        if side not in {"BUY", "SELL"}:
            raise ValueError("Unknown fill side")
        fees = FeeBreakdown() if fees is None else fees
        if not isinstance(fees, FeeBreakdown):
            raise TypeError("fees must be FeeBreakdown")
        sign = 1 if side == "BUY" else -1
        # Validate the entire cash/quantity change before recording any leg.
        if self.cash - sign * quantity * price - fees.total < 0:
            raise ValueError("Insufficient cash including transaction fees")
        if self.positions.get((trade_id, symbol), 0) + sign * quantity < 0:
            raise ValueError("Insufficient shares; short selling is disabled")
        self.post(ts, trade_id, LedgerEventType.FILL, cash=-sign * quantity * price,
                  symbol=symbol, quantity=sign * quantity)
        for field, event in (
            ("commission", LedgerEventType.COMMISSION_PAID),
            ("stamp_duty", LedgerEventType.STAMP_DUTY_PAID),
            ("transfer_fee", LedgerEventType.TRANSFER_FEE_PAID),
            ("handling_fee", LedgerEventType.HANDLING_FEE_PAID),
        ):
            amount = getattr(fees, field)
            if amount:
                self.post(ts, trade_id, event, cash=-amount, symbol=symbol)

    def holdings(self, trade_id):
        return {symbol: q for (tid, symbol), q in self.positions.items() if tid == trade_id and q}

    def equity(self, marks, receivables=ZERO):
        return self.cash + receivables + sum(
            (q * marks[symbol] for (_, symbol), q in self.positions.items() if q), ZERO
        )
