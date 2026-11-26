from datetime import date, datetime, timezone
from decimal import Decimal
import unittest

from etf_arb.portfolio.ledger import (
    ArbitrageTrade,
    LedgerEvent,
    LedgerEventType,
    PeriodPnlSummary,
    PortfolioLedger,
)


class PortfolioLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ledger = PortfolioLedger()

    def test_open_and_settle_trade(self) -> None:
        self.ledger.open_trade(
            trade_id="cycle-1",
            opened_at=datetime(2026, 6, 30, 10, 0),
        )
        self.ledger.settle_trade(
            trade_id="cycle-1",
            settled_at=datetime(2026, 7, 2, 14, 30),
            pnl=Decimal("125.50"),
        )

        trade = self.ledger.trades[0]
        self.assertTrue(trade.is_settled)
        self.assertEqual(trade.pnl, Decimal("125.50"))
        self.assertEqual(trade.settled_at, datetime(2026, 7, 2, 14, 30))

    def test_summary_filters_by_open_date_not_settlement_date(self) -> None:
        self.ledger.open_trade(
            trade_id="june-trade",
            opened_at=datetime(2026, 6, 30, 10, 0),
        )
        self.ledger.settle_trade(
            trade_id="june-trade",
            settled_at=datetime(2026, 7, 2, 14, 30),
            pnl=Decimal("500"),
        )
        self.ledger.open_trade(
            trade_id="pending-trade",
            opened_at=datetime(2026, 6, 30, 13, 0),
        )
        self.ledger.open_trade(
            trade_id="july-trade",
            opened_at=datetime(2026, 7, 1, 10, 0),
        )

        summary = self.ledger.summarize_pnl(
            date(2026, 6, 1),
            date(2026, 6, 30),
        )

        self.assertEqual(summary.opened_count, 2)
        self.assertEqual(summary.settled_count, 1)
        self.assertEqual(summary.pending_count, 1)
        self.assertEqual(summary.settled_net_pnl, Decimal("500"))
        self.assertIsNone(summary.final_net_pnl)

    def test_total_net_pnl_rejects_pending_trade(self) -> None:
        self.ledger.open_trade(
            trade_id="pending",
            opened_at=datetime(2026, 6, 30, 10, 0),
        )

        with self.assertRaisesRegex(ValueError, "仍有 1 笔套利未完成"):
            self.ledger.total_net_pnl(
                date(2026, 6, 1),
                date(2026, 6, 30),
            )

    def test_total_net_pnl_returns_zero_for_empty_period(self) -> None:
        self.assertEqual(
            self.ledger.total_net_pnl(
                date(2026, 6, 1),
                date(2026, 6, 30),
            ),
            Decimal("0"),
        )

    def test_duplicate_open_is_rejected(self) -> None:
        self.ledger.open_trade(
            trade_id="cycle-1",
            opened_at=datetime(2026, 6, 30, 10, 0),
        )

        with self.assertRaisesRegex(ValueError, "交易已登记"):
            self.ledger.open_trade(
                trade_id="cycle-1",
                opened_at=datetime(2026, 6, 30, 10, 1),
            )

    def test_unknown_trade_settlement_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "未找到套利交易"):
            self.ledger.settle_trade(
                trade_id="unknown",
                settled_at=datetime(2026, 6, 30, 10, 0),
                pnl=Decimal("1"),
            )

    def test_duplicate_settlement_is_rejected(self) -> None:
        self.ledger.open_trade(
            trade_id="cycle-1",
            opened_at=datetime(2026, 6, 30, 10, 0),
        )
        self.ledger.settle_trade(
            trade_id="cycle-1",
            settled_at=datetime(2026, 6, 30, 11, 0),
            pnl=Decimal("1"),
        )

        with self.assertRaisesRegex(ValueError, "已完成结算"):
            self.ledger.settle_trade(
                trade_id="cycle-1",
                settled_at=datetime(2026, 6, 30, 12, 0),
                pnl=Decimal("2"),
            )

    def test_invalid_trade_time_and_pnl_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "无时区"):
            ArbitrageTrade(
                trade_id="cycle-1",
                opened_at=datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc),
            )

        with self.assertRaisesRegex(ValueError, "不能早于"):
            ArbitrageTrade(
                trade_id="cycle-2",
                opened_at=datetime(2026, 6, 30, 10, 0),
                settled_at=datetime(2026, 6, 30, 9, 0),
                pnl=Decimal("1"),
            )

        with self.assertRaisesRegex(ValueError, "有限数值"):
            ArbitrageTrade(
                trade_id="cycle-3",
                opened_at=datetime(2026, 6, 30, 10, 0),
                settled_at=datetime(2026, 6, 30, 11, 0),
                pnl=Decimal("NaN"),
            )

    def test_to_closed_trades_contains_only_settled_trades(self) -> None:
        self.ledger.open_trade(
            trade_id="settled",
            opened_at=datetime(2026, 6, 30, 10, 0),
        )
        self.ledger.settle_trade(
            trade_id="settled",
            settled_at=datetime(2026, 6, 30, 11, 0),
            pnl=Decimal("10"),
        )
        self.ledger.open_trade(
            trade_id="pending",
            opened_at=datetime(2026, 6, 30, 12, 0),
        )

        closed = self.ledger.to_closed_trades()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0].trade_id, "settled")
        self.assertEqual(closed[0].pnl, Decimal("10"))

    def test_record_rejects_out_of_order_events(self) -> None:
        self.ledger.record(
            LedgerEvent(
                ts=datetime(2026, 6, 30, 10, 1),
                event_type=LedgerEventType.FILL,
                reference="fill-1",
            )
        )

        with self.assertRaisesRegex(ValueError, "timestamp order"):
            self.ledger.record(
                LedgerEvent(
                    ts=datetime(2026, 6, 30, 10, 0),
                    event_type=LedgerEventType.FILL,
                    reference="fill-2",
                )
            )


if __name__ == "__main__":
    unittest.main()
