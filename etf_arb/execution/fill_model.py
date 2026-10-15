"""Deterministic execution at the latest usable top-of-book quote."""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from etf_arb.domain import Quote, QuoteStatus
from etf_arb.rules.a_share import (
    CONTINUOUS_AUCTION_SESSIONS,
    is_a_share_rule_trading_time,
    is_continuous_auction_time,
)
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

    def __init__(self, *, security_rules: RuleRepository | None = None) -> None:
        self._security_rules = security_rules

    def execute(self, order: Order, market_data: tuple[Quote, ...]) -> ExecutionResult:
        # Buy/sell orders can only execute during A-share continuous auction.
        # This also blocks lunch break and the 14:57 closing call auction.
        if not is_continuous_auction_time(order.submitted_at):
            return ExecutionResult(
                order=order,
                status=ExecutionStatus.NO_FILL,
                reason="Order submitted outside continuous-auction trading hours.",
            )

        if self._security_rules is not None:
            rule = self._security_rules.get_security_rule(
                order.security_code, order.submitted_at.date()
            )
            allowed = (
                is_a_share_rule_trading_time(rule, order.submitted_at)
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
        session_start = next(
            start
            for start, end in CONTINUOUS_AUCTION_SESSIONS
            if start <= clock < end
        )
        candidates = sorted(
            (
                quote for quote in market_data
                if quote.security_code == order.security_code
                and is_continuous_auction_time(quote.ts)
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
