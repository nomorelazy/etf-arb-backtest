"""Read-only cost contributions and display filtering for per-stock reports."""

from datetime import datetime
from decimal import Decimal

ZERO = Decimal(0)
COST_FIELDS = (
    "etf_shares", "stock_fees", "gross_price_pnl", "fee_only",
    "etf_cost_impact", "etf_cost_impact_per_share", "etf_cost_impact_ratio",
)


def decimal(value):
    if value is None or value == "":
        return None
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite stock cost amount")
    return result


def timestamp(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def cost_metrics(pnl, fees, shares, basket_value, *, price_only=False):
    """Positive cost impact is adverse, including reduced redemption proceeds.

    Adding the recorded fees back recovers the gross price change for the price-only view.
    Never add these explanatory amounts to account cash or total trade PnL.
    """
    pnl, fees, shares, basket = map(decimal, (pnl, fees, shares, basket_value))
    if fees is not None and fees < ZERO:
        raise ValueError("Stock fees must be nonnegative")
    gross = pnl + fees if pnl is not None and fees is not None else None
    basis = gross if price_only else pnl
    impact = -basis if basis is not None else None
    return dict(stock_fees=fees, gross_price_pnl=gross,
                # Compare exact amounts before display rounding. Unknown is not zero.
                fee_only=gross == ZERO and fees is not None and fees > ZERO,
                etf_cost_impact=impact,
                etf_cost_impact_per_share=(impact / shares if impact is not None and shares is not None and shares > ZERO else None),
                etf_cost_impact_ratio=(impact / basket if impact is not None and basket is not None and basket > ZERO else None))


def with_cost_metrics(data, trades=(), *, price_only=False):
    """Keep every source row; HTML alone hides fee-only rows. Never mutate input."""
    shares_by_id = {row["trade_id"]: row.get("etf_shares") for row in trades}
    rows = []
    for original in data["rows"]:
        row = dict(original)
        shares = row.get("etf_shares")
        if shares in (None, ""):
            shares = shares_by_id.get(row["trade_id"])
        manager, investor = (decimal(row.get(key)) for key in ("manager_fees", "investor_stock_fees"))
        fees = manager + investor if manager is not None and investor is not None else None
        row.update(etf_shares=shares)
        row.update(cost_metrics(row.get("stock_pnl"), fees, shares, row.get("basket_value"), price_only=price_only))
        rows.append(row)
    return dict(data, rows=rows, cost_schema_version=1,
                cost_basis="signal_iopv_times_etf_shares",
                cost_sign=("positive_increases_creation_price_cost_or_reduces_redemption_gross_proceeds"
                           if price_only else "positive_increases_creation_cost_or_reduces_redemption_net_proceeds"),
                fee_only_policy="hide_from_html_detail_only_keep_cash_fees_pnl_and_all_csv_rows")
