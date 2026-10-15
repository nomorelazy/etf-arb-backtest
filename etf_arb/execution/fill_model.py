"""Deterministic execution at the latest usable top-of-book quote."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from etf_arb.domain import Quote, QuoteStatus
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
    """

    def execute(self, order: Order, market_data: tuple[Quote, ...]) -> ExecutionResult:
        candidates = sorted(
            (
                quote for quote in market_data
                if quote.security_code == order.security_code
                and quote.ts <= order.submitted_at
            ),
            key=lambda quote: quote.ts,
            reverse=True,
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
                ts=quote.ts,
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
