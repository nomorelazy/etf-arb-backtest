"""Six-column completed-trade record, preserving the original portfolio API."""
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from etf_arb.domain import Side

@dataclass(frozen=True)
class TradeRecord:
    """一笔已完成申赎套利交易的汇总记录。

    ts：最初发起套利的时间（T 日），也是账本显示和排序采用的时间。
        即使 T+2 才完成结算，这个字段仍保留 T 日，不改成 T+2。
    side：Side.CREATE（申购）或 Side.REDEEM（赎回）。
    amount：该笔申赎对应的总金额，单位元，由外部按既定价格口径传入。
    etf_shares：该笔对应的 ETF 份额数，不是最小申赎单位的倍数。
    net_pnl：整笔交易完成并计入费用后的净盈亏，单位元；亏损传负数。
    settled_at：完成所有交易腿及最终结算的真实时间（如 T+2）。旧接口
        可省略；新调用建议使用 record_settled_trade 显式传入两个时间。

    金额和盈亏使用 Decimal；交易结果根据盈亏自动派生，零盈亏为持平。
    时间均采用无时区的北京时间。本对象用于结算后按发起日归属交易结果，
    不代表最终盈亏在 T 日已知，不根据申赎金额或折溢价率推算最终盈亏。
    """

    ts: datetime
    side: Side
    amount: Decimal
    etf_shares: int
    net_pnl: Decimal
    settled_at: datetime | None = None
    trade_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.ts, datetime):
            raise TypeError("ts must be a datetime.")
        if self.ts.tzinfo is not None:
            raise ValueError("ts must be timezone-naive Beijing local time.")
        if self.settled_at is not None:
            if not isinstance(self.settled_at, datetime):
                raise TypeError("settled_at must be a datetime.")
            if self.settled_at.tzinfo is not None:
                raise ValueError("settled_at must be timezone-naive Beijing local time.")
            if self.settled_at < self.ts:
                raise ValueError("settled_at cannot be earlier than the arbitrage opening time.")
        try:
            object.__setattr__(self, "side", Side(self.side))
        except (TypeError, ValueError) as error:
            raise ValueError("side must be CREATE or REDEEM.") from error
        for name in ("amount", "net_pnl"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"{name} must be a Decimal.")
            if not value.is_finite():
                raise ValueError(f"{name} must be finite.")
        if self.amount <= 0:
            raise ValueError("amount must be positive.")
        if type(self.etf_shares) is not int or self.etf_shares <= 0:
            raise ValueError("etf_shares must be a positive integer.")

    @property
    def opened_at(self) -> datetime:
        """套利发起时间；保留 ts 字段以兼容已有调用。"""
        return self.ts

    @property
    def pnl(self) -> Decimal:
        return self.net_pnl

    @property
    def is_settled(self) -> bool:
        # record_trade is the legacy API for a completed result. Older callers
        # may omit its actual settlement timestamp; never infer a later date.
        return True

    @property
    def trade_type(self) -> str:
        return "申购" if self.side is Side.CREATE else "赎回"

    @property
    def trade_result(self) -> str:
        if self.net_pnl > 0:
            return "赚钱"
        if self.net_pnl < 0:
            return "亏钱"
        return "持平"
