"""Deterministic execution at the latest usable top-of-book quote."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from etf_arb.domain import Quote, QuoteStatus
from etf_arb.rules.a_share import (
    CONTINUOUS_AUCTION_SESSIONS,
)
from etf_arb.rules.trading_time import TradingTimeRule
from etf_arb.rules.models import RuleRepository

from etf_arb.execution.models import (
    ExecutionResult,
    ExecutionStatus,
    Fill,
    Order,
    OrderSide,
)


class FillModel(Protocol):
    def execute(self, order: Order, market_data: tuple[Quote, ...]) -> ExecutionResult:
        """Return fills without mutating the order or other orders."""
        ...


class NextQuoteFillModel:
    """Fill at the latest usable top-of-book quote at order submission.

    A buy uses the quote's ask and a sell uses its bid. The quote is already
    the simulated execution price, so this model does not add a basis-point
    adjustment, infer market impact, or use ``last`` as a substitute for a
    missing side of the book. Only quotes at or before order submission are
    eligible. Because :class:`Quote` does not carry displayed bid/ask
    quantities, a usable quote fills the entire order quantity.

    When a security-rule repository is supplied, day-specific suspension,
    delisting expiry, and direction restrictions are enforced before filling.
    """

    def __init__(self, *, security_rules: RuleRepository | None = None,
                 trading_times: TradingTimeRule | None = None) -> None:
        self._security_rules = security_rules
        self._trading_times = TradingTimeRule() if trading_times is None else trading_times

    def execute(self, order: Order, market_data: tuple[Quote, ...]) -> ExecutionResult:
        if order.submitted_at.tzinfo is not None:
            raise ValueError("Order timestamps must be timezone-naive Beijing time")
        if not self._trading_times.allows(order.submitted_at.time()):
            return ExecutionResult(
                order=order,
                status=ExecutionStatus.NO_FILL,
                reason="Order submitted outside configured trading windows.",
            )

        if self._security_rules is not None:
            rule = self._security_rules.get_security_rule(
                order.security_code, order.submitted_at.date()
            )
            allowed = (
                rule.is_trading_time(order.submitted_at.time())
                and (
                    rule.can_buy() if order.side is OrderSide.BUY else rule.can_sell()
                )
            )
            if not allowed:
                return ExecutionResult(
                    order=order,
                    status=ExecutionStatus.REJECTED,
                    reason="Security trading rule prohibits this order (suspended or not tradable).",
                )

        # Never fill an afternoon order using a quote from the morning,
        # a previous day, or outside the same continuous-auction session.
        clock = order.submitted_at.time()
        session_start = next((start
            for start, end in CONTINUOUS_AUCTION_SESSIONS
            if start <= clock <= end), None)
        if session_start is None:
            return ExecutionResult(order=order, status=ExecutionStatus.NO_FILL,
                                   reason="No source quote session for the configured order time.")
        candidates = sorted(
            (
                quote for quote in market_data
                if quote.security_code == order.security_code
                and quote.ts.tzinfo is None
                and any(start <= quote.ts.time() <= end for start, end in CONTINUOUS_AUCTION_SESSIONS)
                and quote.ts.date() == order.submitted_at.date()
                and quote.ts.time() >= session_start
                and quote.ts <= order.submitted_at
            ),
            key=lambda quote: quote.ts,
            reverse=True,
        )
        # An explicit suspension quote dominates older normal quotes.
        # Never "rewind" to a tradable quote when the latest snapshot is halted.
        if candidates and candidates[0].status is QuoteStatus.SUSPENDED:
            return ExecutionResult(
                order=order,
                status=ExecutionStatus.NO_FILL,
                reason="Latest quote indicates trading suspension.",
            )

        for quote in candidates:
            if quote.status not in (
                QuoteStatus.NORMAL,
                QuoteStatus.LIMIT_UP,
                QuoteStatus.LIMIT_DOWN,
            ):
                continue
            if order.side is OrderSide.BUY and quote.status is QuoteStatus.LIMIT_UP:
                continue
            if order.side is OrderSide.SELL and quote.status is QuoteStatus.LIMIT_DOWN:
                continue

            reference_price = self._reference_price(order.side, quote)
            if reference_price is None:
                continue

            fill = Fill(
                order_id=order.order_id,
                security_code=order.security_code,
                side=order.side,
                quantity=order.quantity,
                price=reference_price,
                ts=order.submitted_at,
                quote_ts=quote.ts,
            )
            return ExecutionResult(order=order, status=ExecutionStatus.FILLED, fills=(fill,))

        return ExecutionResult(
            order=order,
            status=ExecutionStatus.NO_FILL,
            reason="No usable quote at or before order submission.",
        )

    @staticmethod
    def _reference_price(side: OrderSide, quote: Quote) -> Decimal | None:
        price = quote.ask if side is OrderSide.BUY else quote.bid
        if price is None:
            return None
        value = Decimal(str(price))
        if not value.is_finite() or value <= 0:
            return None
        return value


__all__ = ["FillModel", "NextQuoteFillModel"]
