import csv
from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from etf_arb.domain import Side
from etf_arb.portfolio import LedgerEvent, LedgerEventType, PortfolioLedger, TradeRecord


def make_trade(**changes) -> TradeRecord:
    values = {
        "ts": datetime(2025, 1, 2, 10),
        "side": Side.CREATE,
        "amount": Decimal("1000000.00"),
        "etf_shares": 1000000,
        "net_pnl": Decimal("123.45"),
    }
    values.update(changes)
    return TradeRecord(**values)


class PortfolioLedgerTests(unittest.TestCase):
    def test_out_of_order_records_are_sorted_by_full_timestamp(self) -> None:
        early = make_trade(ts=datetime(2025, 1, 2, 14))
        middle = make_trade(ts=datetime(2025, 1, 3, 9, 30))
        late = make_trade(ts=datetime(2025, 1, 3, 10))
        ledger = PortfolioLedger()
        for trade in (late, early, middle):
            ledger.record_trade(trade)
        self.assertEqual(ledger.trades, (early, middle, late))

    def test_same_timestamp_keeps_insertion_order(self) -> None:
        first = make_trade(side=Side.REDEEM)
        second = make_trade(side=Side.CREATE)
        ledger = PortfolioLedger()
        ledger.record_trade(first)
        ledger.record_trade(second)
        self.assertEqual(ledger.trades, (first, second))

    def test_delayed_settlement_is_reported_and_sorted_under_opening_day(self) -> None:
        ledger = PortfolioLedger()
        # 后发起、先结算的交易先入账；T 日的交易直到 T+2 才结算补录。
        later_opening = ledger.record_settled_trade(
            opened_at=datetime(2025, 1, 6, 10),
            settled_at=datetime(2025, 1, 6, 14),
            side=Side.REDEEM,
            amount=Decimal("2000000"),
            etf_shares=2000000,
            net_pnl=Decimal("50"),
        )
        opening = datetime(2025, 1, 3, 10)
        settlement = datetime(2025, 1, 7, 14)
        delayed = ledger.record_settled_trade(
            opened_at=opening,
            settled_at=settlement,
            side=Side.CREATE,
            amount=Decimal("1000000"),
            etf_shares=1000000,
            net_pnl=Decimal("-120.50"),
        )
        self.assertEqual(ledger.trades, (delayed, later_opening))
        self.assertEqual(delayed.ts, opening)
        self.assertEqual(delayed.opened_at, opening)
        self.assertEqual(delayed.settled_at, settlement)
        self.assertEqual(ledger.to_rows()[0]["时间"], "2025-01-03 10:00:00")
        self.assertEqual(ledger.to_rows()[0]["交易结果"], "亏钱")
        self.assertEqual(ledger.to_rows()[0]["交易盈亏（元）"], "-120.50")
        with TemporaryDirectory() as temp:
            target = ledger.export_csv(Path(temp) / "delayed.csv")
            with target.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.DictReader(file))
        self.assertEqual(rows[0]["时间"], "2025-01-03 10:00:00")
        self.assertEqual(rows[1]["时间"], "2025-01-06 10:00:00")

    def test_real_account_events_keep_actual_settlement_timestamp(self) -> None:
        ledger = PortfolioLedger()
        settlement = datetime(2025, 1, 7, 14)
        event = LedgerEvent(settlement, LedgerEventType.CASH_SETTLED, "delayed-trade")
        ledger.record(event)
        ledger.record_trade(make_trade(
            ts=datetime(2025, 1, 3, 10), settled_at=settlement,
        ))
        self.assertEqual(ledger.events[0].ts, settlement)
        self.assertEqual(ledger.to_rows()[0]["时间"], "2025-01-03 10:00:00")

    def test_invalid_settlement_times_are_rejected_without_inserting_trade(self) -> None:
        ledger = PortfolioLedger()
        for settlement, error_type in (
            (None, TypeError),
            (date(2025, 1, 7), TypeError),
            (datetime(2025, 1, 7, tzinfo=timezone.utc), ValueError),
            (datetime(2025, 1, 2), ValueError),
        ):
            with self.subTest(settlement=settlement):
                with self.assertRaises(error_type):
                    ledger.record_settled_trade(
                        opened_at=datetime(2025, 1, 3, 10),
                        settled_at=settlement,
                        side=Side.CREATE,
                        amount=Decimal("1000000"),
                        etf_shares=1000000,
                        net_pnl=Decimal("-120.50"),
                    )
        self.assertEqual(ledger.trades, ())

    def test_trade_result_is_derived_from_signed_pnl(self) -> None:
        for pnl, label in (("12.30", "赚钱"), ("-4.50", "亏钱"), ("0", "持平")):
            with self.subTest(pnl=pnl):
                trade = make_trade(net_pnl=Decimal(pnl))
                self.assertEqual(trade.trade_result, label)
                self.assertEqual(trade.net_pnl, Decimal(pnl))
        self.assertEqual(make_trade(side=Side.CREATE).trade_type, "申购")
        self.assertEqual(make_trade(side="REDEEM").trade_type, "赎回")

    def test_records_cannot_be_modified_or_reordered_externally(self) -> None:
        ledger = PortfolioLedger()
        trade = make_trade()
        ledger.record_trade(trade)
        self.assertIsInstance(ledger.trades, tuple)
        with self.assertRaises(FrozenInstanceError):
            trade.ts = datetime(2024, 1, 1)
        rows = ledger.to_rows()
        rows[0]["交易盈亏（元）"] = "999"
        self.assertEqual(ledger.trades[0].net_pnl, Decimal("123.45"))

    def test_csv_contains_requested_columns_sorted_and_preserves_loss(self) -> None:
        ledger = PortfolioLedger()
        ledger.record_trade(make_trade(ts=datetime(2025, 1, 2, 11)))
        ledger.record_trade(make_trade(
            side=Side.REDEEM,
            amount=Decimal("999999.1234"),
            net_pnl=Decimal("-15.6789"),
        ))
        with TemporaryDirectory() as temp:
            target = ledger.export_csv(Path(temp) / "nested" / "ledger.csv")
            self.assertTrue(target.read_bytes().startswith(b"\xef\xbb\xbf"))
            with target.open(encoding="utf-8-sig", newline="") as file:
                reader = csv.DictReader(file)
                rows = list(reader)
                self.assertEqual(reader.fieldnames, [
                    "时间", "交易类型", "交易额度（元）", "交易单位（ETF份）",
                    "交易结果", "交易盈亏（元）",
                ])
        self.assertEqual(rows[0], {
            "时间": "2025-01-02 10:00:00",
            "交易类型": "赎回",
            "交易额度（元）": "999999.1234",
            "交易单位（ETF份）": "1000000",
            "交易结果": "亏钱",
            "交易盈亏（元）": "-15.6789",
        })
        self.assertEqual(rows[1]["时间"], "2025-01-02 11:00:00")

    def test_empty_ledger_exports_header(self) -> None:
        ledger = PortfolioLedger()
        with TemporaryDirectory() as temp:
            target = ledger.export_csv(Path(temp) / "empty.csv")
            with target.open(encoding="utf-8-sig", newline="") as file:
                rows = list(csv.reader(file))
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]), 6)
        self.assertEqual(ledger.trades, ())

    def test_invalid_trade_inputs_are_rejected(self) -> None:
        invalid = (
            ({"ts": date(2025, 1, 2)}, TypeError),
            ({"ts": datetime(2025, 1, 2, tzinfo=timezone.utc)}, ValueError),
            ({"side": "BUY"}, ValueError),
            ({"amount": Decimal("0")}, ValueError),
            ({"amount": Decimal("-1")}, ValueError),
            ({"amount": 1.01}, TypeError),
            ({"net_pnl": "-10"}, TypeError),
            ({"etf_shares": 0}, ValueError),
            ({"etf_shares": -1}, ValueError),
            ({"etf_shares": 0.5}, ValueError),
            ({"etf_shares": True}, ValueError),
        )
        for changes, error_type in invalid:
            with self.subTest(changes=changes):
                with self.assertRaises(error_type):
                    make_trade(**changes)
        for field in ("amount", "net_pnl"):
            for value in ("NaN", "sNaN", "Infinity", "-Infinity"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        make_trade(**{field: Decimal(value)})
        with self.assertRaises(TypeError):
            PortfolioLedger().record_trade({})

    def test_original_event_interface_is_preserved(self) -> None:
        ledger = PortfolioLedger()
        event = LedgerEvent(datetime(2025, 1, 2, 10), LedgerEventType.FILL, "order-1")
        ledger.record(event)
        ledger.record_trade(make_trade(ts=datetime(2025, 1, 2, 9)))
        self.assertEqual(ledger.events, [event])
        with self.assertRaises(ValueError):
            ledger.record(LedgerEvent(datetime(2025, 1, 2, 9), LedgerEventType.FILL, "order-0"))


if __name__ == "__main__":
    unittest.main()
