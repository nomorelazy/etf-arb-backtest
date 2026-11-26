from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pyarrow as pa
import pyarrow.parquet as pq

from etf_arb.data import (
    MinuteCloseConflictError,
    ParquetMinuteCloseRepository,
)


def write_parquet(path: Path, rows: list[tuple[datetime, str, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "time": pa.array([row[0] for row in rows], type=pa.timestamp("us")),
            "thscode": pa.array([row[1] for row in rows], type=pa.large_string()),
            "close": pa.array([row[2] for row in rows], type=pa.float64()),
        }
    )
    pq.write_table(table, path)


class MinuteCloseRepositoryTests(unittest.TestCase):
    def test_scans_only_month_parquet_and_deduplicates_equal_overlaps(self) -> None:
        t0 = datetime(2025, 1, 2, 9, 30)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            month = root / "2025-01"
            first = month / "etf_first.parquet"
            second = month / "chunk_000_second.parquet"
            write_parquet(
                first,
                [(t0, "159338.SZ", 0.954), (t0 + timedelta(minutes=1), "159338.SZ", 0.952)],
            )
            write_parquet(
                second,
                [
                    (t0 + timedelta(minutes=1), "159338.SZ", 0.952),
                    (t0 + timedelta(minutes=2), "159338.SZ", 0.951),
                ],
            )
            (root / "unrelated.csv").write_text("ignored", encoding="utf-8")
            unrelated = root / "reports"
            unrelated.mkdir()
            write_parquet(
                unrelated / "ignored.parquet",
                [(t0, "OTHER.SZ", 1.0)],
            )

            repository = ParquetMinuteCloseRepository(root, catalog_path=root / "catalog.sqlite3")
            report = repository.scan()
            bars = repository.get_closes("159338.SZ")

        self.assertEqual(report.files_found, 2)
        self.assertEqual(report.files_indexed, 0)
        self.assertEqual(report.files_unchanged, 2)
        self.assertEqual(report.rows_indexed, 4)
        self.assertEqual(report.securities_indexed, 1)
        self.assertEqual(len(bars), 3)
        self.assertEqual([bar.close for bar in bars], [Decimal("0.954"), Decimal("0.952"), Decimal("0.951")])
        self.assertEqual(len(bars[1].source_files), 2)
        self.assertEqual(bars[1].time, t0 + timedelta(minutes=1))

    def test_incremental_scan_records_per_file_and_security_inventory(self) -> None:
        t0 = datetime(2025, 1, 2, 9, 30)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "2025-01" / "etf.parquet"
            write_parquet(path, [(t0, "512760.SH", 1.25)])
            repository = ParquetMinuteCloseRepository(
                root,
                catalog_path=root / "catalog.sqlite3",
                auto_scan=False,
            )
            first = repository.scan()
            inventory = repository.inventory()
            codes = repository.security_codes()

        self.assertEqual(first.files_indexed, 1)
        self.assertEqual(first.rows_indexed, 1)
        self.assertEqual(first.securities_indexed, 1)
        self.assertEqual(codes, ("512760.SH",))
        self.assertEqual(inventory[0]["relative_path"], "2025-01/etf.parquet")
        self.assertEqual(inventory[0]["schema"][0]["name"], "time")

    def test_conflicting_overlapping_close_raises(self) -> None:
        t0 = datetime(2025, 1, 2, 9, 30)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            write_parquet(root / "2025-01" / "a.parquet", [(t0, "159338.SZ", 0.954)])
            write_parquet(root / "2025-01" / "b.parquet", [(t0, "159338.SZ", 0.955)])
            repository = ParquetMinuteCloseRepository(root, catalog_path=root / "catalog.sqlite3")
            with self.assertRaises(MinuteCloseConflictError):
                repository.get_closes("159338.SZ")

    def test_timestamp_us_bars_are_filtered_after_loading_to_auction_sessions(self):
        day = (2025, 1, 2)
        values = (
            (datetime(*day, 9, 29, 59), 1.00),
            (datetime(*day, 9, 30), 1.01),
            (datetime(*day, 11, 29, 59, 999999), 1.02),
            (datetime(*day, 11, 30), 1.03),
            (datetime(*day, 12, 0), 1.04),
            (datetime(*day, 12, 59, 59), 1.05),
            (datetime(*day, 13, 0), 1.06),
            (datetime(*day, 14, 56, 59, 999999), 1.07),
            (datetime(*day, 14, 57), 1.08),
        )
        with TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "2025-01" / "etf.parquet"
            write_parquet(path, [(at, "159338.SZ", price) for at, price in values])
            self.assertEqual(pq.read_schema(path).field("time").type, pa.timestamp("us"))
            repo = ParquetMinuteCloseRepository(root, catalog_path=root / "catalog.sqlite3")
            raw = repo.get_closes("159338.SZ")
            tradable = repo.get_closes("159338.SZ", continuous_auction_only=True)
        self.assertEqual([b.time for b in raw], [at for at, _ in values])
        self.assertEqual(
            [b.time for b in tradable],
            [values[i][0] for i in (1, 2, 6, 7)],
        )
        self.assertTrue(all(b.time.tzinfo is None for b in tradable))

    def test_aware_query_bounds_are_not_converted(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            write_parquet(
                root / "2025-01" / "etf.parquet",
                [(datetime(2025, 1, 2, 9, 30), "159338.SZ", 1.0)],
            )
            repo = ParquetMinuteCloseRepository(root, catalog_path=root / "catalog.sqlite3")
            with self.assertRaisesRegex(ValueError, "timezone-naive"):
                repo.get_closes(
                    "159338.SZ",
                    start=datetime(2025, 1, 2, 1, 30, tzinfo=timezone.utc),
                )
            with self.assertRaisesRegex(ValueError, "timezone-naive"):
                repo.get_at_or_before(
                    "159338.SZ",
                    datetime(2025, 1, 2, 1, 30, tzinfo=timezone.utc),
                )

    def test_get_at_or_before_uses_naive_beijing_timestamp(self) -> None:
        t0 = datetime(2025, 1, 2, 9, 30)
        with TemporaryDirectory() as temp:
            root = Path(temp)
            write_parquet(
                root / "2025-01" / "etf.parquet",
                [
                    (t0, "159338.SZ", 0.954),
                    (t0 + timedelta(minutes=1), "159338.SZ", 0.952),
                ],
            )
            repository = ParquetMinuteCloseRepository(root, catalog_path=root / "catalog.sqlite3")
            bar = repository.get_at_or_before(
                "159338.SZ", datetime(2025, 1, 2, 9, 30)
            )

        self.assertIsNotNone(bar)
        assert bar is not None
        self.assertEqual(bar.time, t0)
        self.assertEqual(bar.close, Decimal("0.954"))


if __name__ == "__main__":
    unittest.main()
