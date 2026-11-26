"""Fixed historical price-loss calibration for the 159338 limit-aware signal threshold.

Source: 159338_双0.17%_涨跌停纯价差_原指标拆分（剔除2025-04-07）.xlsx
SHA-256: 623b58ddb7c7a066503466f5308be396eadbf8f955979110e244ec6046ade365
Sample window: 2025-01-02 through 2026-07-22, excluding 2025-04-07 openings.
Every original trade-stock row counts; pure price P&L excludes taxes and fees.
The fixed rates describe the calibration period; overlapping backtests are in-sample.
"""

from bisect import bisect_right
from dataclasses import dataclass
from decimal import Decimal

from etf_arb.domain import Side


CALIBRATED_ETF = "159338"
BUCKET_MINIMA = (0, 1000, 3000, 5000, 10000, 20000)

# (sample row count, sum of signal-time stock value, sum of signed price P&L).
# Amounts are CNY, and the rows follow BUCKET_MINIMA.
SAMPLES = {
    "申购涨停·深市": (
        (2, 1672, 32),
        (84, 165889, -7322),
        (90, 357168, -13931),
        (75, 530826, -41135),
        (40, 555707, -34975),
        (11, 317544, -8650),
    ),
    "申购涨停·沪市": (
        (0, 0, 0),
        (41, 95980, -1254),
        (66, 267768, -15966),
        (72, 517749, -9729),
        (45, 659081, -35527),
        (18, 588533, -16801),
    ),
    "赎回跌停": (
        (0, 0, 0),
        (27, 56325, -1000),
        (23, 93618, -4057),
        (42, 304638, -10252),
        (20, 271058, -9331),
        (6, 244383, 1039),
    ),
}


@dataclass(frozen=True)
class LimitLossEstimate:
    group: str
    bucket_min: int
    bucket_max: int | None
    sample_count: int
    used_group_fallback: bool
    loss_per_100: Decimal
    expected_loss: Decimal


def estimate_limit_loss(side: Side, exchange: str, stock_value: Decimal) -> LimitLossEstimate:
    """Estimate loss for one confirmed directional limit hit at its current trade size.

    sample_count is the count in the selected value bucket (zero for a fallback).
    Empty buckets use the value-weighted rate of their entire group. A group or
    bucket with nonnegative signed historical P&L contributes no extra threshold.
    """
    side = Side(side)
    if exchange not in {"SH", "SZ"}:
        raise ValueError(f"Unsupported stock exchange for 159338 calibration: {exchange}")
    stock_value = Decimal(str(stock_value))
    if not stock_value.is_finite() or stock_value < 0:
        raise ValueError("Signal stock value must be finite and nonnegative")
    group = "赎回跌停"
    if side is Side.CREATE:
        group = "申购涨停·深市" if exchange == "SZ" else "申购涨停·沪市"
    index = bisect_right(BUCKET_MINIMA, stock_value) - 1
    buckets = SAMPLES[group]
    count, total_value, total_pnl = buckets[index]
    if count == 0:
        total_value = sum(row[1] for row in buckets)
        total_pnl = sum(row[2] for row in buckets)
    loss_amount = max(0, -total_pnl)
    return LimitLossEstimate(
        group=group,
        bucket_min=BUCKET_MINIMA[index],
        bucket_max=BUCKET_MINIMA[index + 1] if index + 1 < len(BUCKET_MINIMA) else None,
        sample_count=count,
        used_group_fallback=count == 0,
        loss_per_100=Decimal(100 * loss_amount) / total_value,
        expected_loss=stock_value * loss_amount / total_value,
    )
