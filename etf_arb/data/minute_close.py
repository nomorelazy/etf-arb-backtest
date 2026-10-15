"""Catalog and query iFinD minute-close Parquet files.

The source data stays in its existing ``YYYY-MM/*.parquet`` layout. A small
SQLite catalog records file metadata and per-file security/time ranges, so the
store can discover new or changed files without copying tens of millions of
market-data rows into a second database.
"""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


_MONTH_DIRECTORY = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")
_REQUIRED_COLUMNS = {"time", "thscode", "close"}


class MinuteCloseDataError(ValueError):
    """A minute-close file or its stored rows do not match the data contract."""


class MinuteCloseConflictError(MinuteCloseDataError):
    """Multiple source files contain different closes for one timestamp/code."""


@dataclass(frozen=True)
class MinuteCloseBar:
    """One minute close; ``time`` is a naive Asia/Shanghai local timestamp."""

    time: datetime
    security_code: str
    close: Decimal
    source_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class MinuteCloseScanReport:
    files_found: int
    files_indexed: int
    files_unchanged: int
    files_removed: int
    rows_indexed: int
    securities_indexed: int
    catalog_path: str


class ParquetMinuteCloseRepository:
    """Index and read Parquet data stored in ``<root>/YYYY-MM/*.parquet``.

    Construction performs an incremental scan by default. The catalog defaults
    to a hidden SQLite file inside ``root``; it contains metadata only, while
    original Parquet files remain the source of truth.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        catalog_path: str | Path | None = None,
        auto_scan: bool = True,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"Minute-close data directory does not exist: {self.root}")
        self.catalog_path = (
            Path(catalog_path).expanduser().resolve()
            if catalog_path is not None
            else self._default_catalog_path()
        )
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_catalog()
        if auto_scan:
            self.scan()

    def _default_catalog_path(self) -> Path:
        project_data = Path(__file__).resolve().parents[2] / "data" / "market"
        root_token = hashlib.sha256(str(self.root).casefold().encode("utf-8")).hexdigest()[:12]
        return project_data / f"{self.root.name}_{root_token}_minute_close_catalog.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.catalog_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_catalog(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS minute_close_files (
                    relative_path TEXT PRIMARY KEY,
                    partition_month TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    modified_ns INTEGER NOT NULL,
                    row_count INTEGER NOT NULL,
                    schema_json TEXT NOT NULL,
                    min_time TEXT NOT NULL,
                    max_time TEXT NOT NULL,
                    scanned_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS minute_close_file_securities (
                    relative_path TEXT NOT NULL,
                    security_code TEXT NOT NULL,
                    row_count INTEGER NOT NULL,
                    min_time TEXT NOT NULL,
                    max_time TEXT NOT NULL,
                    PRIMARY KEY (relative_path, security_code),
                    FOREIGN KEY (relative_path)
                        REFERENCES minute_close_files(relative_path)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_minute_close_security_time
                    ON minute_close_file_securities(security_code, min_time, max_time);
                """
            )

    def _discover_files(self) -> list[Path]:
        files: list[Path] = []
        for month_dir in self.root.iterdir():
            if not month_dir.is_dir() or not _MONTH_DIRECTORY.fullmatch(month_dir.name):
                continue
            files.extend(
                path
                for path in month_dir.iterdir()
                if path.is_file() and path.suffix.lower() == ".parquet"
            )
        return sorted(files, key=lambda path: path.relative_to(self.root).as_posix())

    @staticmethod
    def _time_text(value: datetime) -> str:
        if value.tzinfo is not None:
            raise MinuteCloseDataError("Parquet time values must not contain timezone metadata.")
        return value.isoformat(sep=" ", timespec="microseconds")

    @staticmethod
    def _normalize_time(value: datetime) -> datetime:
        if not isinstance(value, datetime):
            raise TypeError("time bounds must be datetime values")
        if value.tzinfo is not None:
            raise ValueError("time bounds must be timezone-naive Beijing local datetimes")
        return value

    def _inspect_file(self, path: Path) -> tuple[dict[str, object], list[tuple[str, int, str, str]]]:
        try:
            parquet = pq.ParquetFile(path)
            schema = parquet.schema_arrow
            names = set(schema.names)
            missing = _REQUIRED_COLUMNS - names
            if missing:
                raise MinuteCloseDataError(
                    f"Missing required columns {sorted(missing)} in {path}"
                )

            time_type = schema.field("time").type
            code_type = schema.field("thscode").type
            close_type = schema.field("close").type
            if not pa.types.is_timestamp(time_type) or time_type.tz is not None:
                raise MinuteCloseDataError(
                    f"time must be a timezone-naive Parquet timestamp in {path}; got {time_type}"
                )
            if not (pa.types.is_string(code_type) or pa.types.is_large_string(code_type)):
                raise MinuteCloseDataError(f"thscode must be a string in {path}; got {code_type}")
            if not (
                pa.types.is_integer(close_type)
                or pa.types.is_floating(close_type)
                or pa.types.is_decimal(close_type)
            ):
                raise MinuteCloseDataError(f"close must be numeric in {path}; got {close_type}")
            if parquet.metadata.num_rows == 0:
                raise MinuteCloseDataError(f"Parquet file contains no rows: {path}")

            keys = parquet.read(columns=["time", "thscode"])
            if keys["time"].null_count or keys["thscode"].null_count:
                raise MinuteCloseDataError(f"time/thscode contains null values in {path}")
            codes = pc.unique(keys["thscode"].combine_chunks()).to_pylist()
            if any(not str(code).strip() or str(code) != str(code).strip() for code in codes):
                raise MinuteCloseDataError(f"thscode contains empty or padded codes in {path}")

            closes = parquet.read(columns=["close"])["close"].combine_chunks()
            if closes.null_count:
                raise MinuteCloseDataError(f"close contains null values in {path}")
            if pa.types.is_floating(close_type) and not pc.all(pc.is_finite(closes)).as_py():
                raise MinuteCloseDataError(f"close contains NaN or infinity values in {path}")
            if not pc.all(pc.greater(closes, 0)).as_py():
                raise MinuteCloseDataError(f"close contains non-positive values in {path}")
            grouped = keys.group_by("thscode").aggregate(
                [("time", "min"), ("time", "max"), ("time", "count")]
            )
            securities: list[tuple[str, int, str, str]] = []
            for row in grouped.to_pylist():
                code = str(row["thscode"]).strip()
                first = row["time_min"]
                last = row["time_max"]
                if not code or first is None or last is None:
                    raise MinuteCloseDataError(f"Invalid security/time key in {path}")
                securities.append(
                    (
                        code,
                        int(row["time_count"]),
                        self._time_text(first),
                        self._time_text(last),
                    )
                )
            if not securities:
                raise MinuteCloseDataError(f"No security rows found in {path}")

            min_time = min(row[2] for row in securities)
            max_time = max(row[3] for row in securities)
            schema_json = json.dumps(
                [
                    {"name": field.name, "type": str(field.type), "nullable": field.nullable}
                    for field in schema
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            stat = path.stat()
            metadata: dict[str, object] = {
                "relative_path": path.relative_to(self.root).as_posix(),
                "partition_month": path.parent.name,
                "file_name": path.name,
                "size_bytes": stat.st_size,
                "modified_ns": stat.st_mtime_ns,
                "row_count": parquet.metadata.num_rows,
                "schema_json": schema_json,
                "min_time": min_time,
                "max_time": max_time,
                "scanned_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
            }
            return metadata, securities
        except MinuteCloseDataError:
            raise
        except (OSError, pa.ArrowException) as error:
            raise MinuteCloseDataError(f"Cannot inspect Parquet file {path}: {error}") from error

    def scan(self, *, force: bool = False) -> MinuteCloseScanReport:
        """Record new/changed files and remove catalog entries for deleted files."""
        files = self._discover_files()
        discovered = {
            path.relative_to(self.root).as_posix(): path
            for path in files
        }
        indexed_count = 0
        unchanged_count = 0
        removed_count = 0

        try:
            with self._connection() as connection:
                existing = {
                    row["relative_path"]: (row["size_bytes"], row["modified_ns"])
                    for row in connection.execute(
                        "SELECT relative_path, size_bytes, modified_ns FROM minute_close_files"
                    )
                }
                stale_paths = set(existing) - set(discovered)
                for stale_path in stale_paths:
                    connection.execute(
                        "DELETE FROM minute_close_files WHERE relative_path = ?", (stale_path,)
                    )
                removed_count = len(stale_paths)

                for relative_path, path in discovered.items():
                    stat = path.stat()
                    previous = existing.get(relative_path)
                    if (
                        not force
                        and previous is not None
                        and previous == (stat.st_size, stat.st_mtime_ns)
                    ):
                        unchanged_count += 1
                        continue

                    metadata, securities = self._inspect_file(path)
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO minute_close_files (
                            relative_path, partition_month, file_name, size_bytes,
                            modified_ns, row_count, schema_json, min_time, max_time, scanned_at
                        ) VALUES (
                            :relative_path, :partition_month, :file_name, :size_bytes,
                            :modified_ns, :row_count, :schema_json, :min_time, :max_time, :scanned_at
                        )
                        """,
                        metadata,
                    )
                    connection.executemany(
                        """
                        INSERT INTO minute_close_file_securities (
                            relative_path, security_code, row_count, min_time, max_time
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            (relative_path, code, count, first, last)
                            for code, count, first, last in securities
                        ],
                    )
                    indexed_count += 1

                totals = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS file_count,
                        COALESCE(SUM(row_count), 0) AS row_count,
                        (SELECT COUNT(DISTINCT security_code)
                         FROM minute_close_file_securities) AS security_count
                    FROM minute_close_files
                    """
                ).fetchone()
        except (OSError, sqlite3.Error) as error:
            raise MinuteCloseDataError(f"Cannot update minute-close catalog: {error}") from error

        return MinuteCloseScanReport(
            files_found=len(files),
            files_indexed=indexed_count,
            files_unchanged=unchanged_count,
            files_removed=removed_count,
            rows_indexed=int(totals["row_count"]),
            securities_indexed=int(totals["security_count"]),
            catalog_path=str(self.catalog_path),
        )

    def inventory(self) -> tuple[dict[str, object], ...]:
        """Return the indexed per-file inventory, ordered by month and name."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT relative_path, partition_month, file_name, size_bytes,
                       row_count, schema_json, min_time, max_time, scanned_at
                FROM minute_close_files
                ORDER BY partition_month, file_name
                """
            ).fetchall()
        return tuple(
            {
                **dict(row),
                "schema": json.loads(row["schema_json"]),
            }
            for row in rows
        )

    def security_codes(self) -> tuple[str, ...]:
        """Return distinct indexed iFinD codes, including exchange suffixes."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT DISTINCT security_code FROM minute_close_file_securities ORDER BY security_code"
            ).fetchall()
        return tuple(row[0] for row in rows)

    def _candidate_files(
        self,
        security_code: str,
        start: datetime | None,
        end: datetime | None,
    ) -> tuple[str, ...]:
        clauses = ["s.security_code = ?"]
        parameters: list[object] = [security_code]
        if start is not None:
            clauses.append("s.max_time >= ?")
            parameters.append(self._time_text(start))
        if end is not None:
            clauses.append("s.min_time <= ?")
            parameters.append(self._time_text(end))
        query = """
            SELECT s.relative_path
            FROM minute_close_file_securities AS s
            WHERE """ + " AND ".join(clauses) + " ORDER BY s.relative_path"
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(row[0] for row in rows)

    @staticmethod
    def _decimal_close(value: object, *, path: Path, code: str, time: datetime) -> Decimal:
        if value is None:
            raise MinuteCloseDataError(f"Missing close for {code} at {time} in {path}")
        try:
            close = Decimal(str(value))
        except (InvalidOperation, ValueError) as error:
            raise MinuteCloseDataError(
                f"Invalid close {value!r} for {code} at {time} in {path}"
            ) from error
        if not close.is_finite() or close <= 0:
            raise MinuteCloseDataError(
                f"Close must be finite and positive for {code} at {time} in {path}: {value!r}"
            )
        return close

    def get_closes(
        self,
        security_code: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        continuous_auction_only: bool = False,
    ) -> tuple[MinuteCloseBar, ...]:
        """Read one security's timezone-naive Beijing-local minute close bars.

        When ``continuous_auction_only`` is true, keep just the A-share
        continuous auction windows [09:30, 11:30) and [13:00, 14:57).
        Conflicting values at the same ``(time, thscode)`` are raised rather
        than silently selected, since overlapping source chunks exist.
        """
        code = str(security_code).strip()
        if not code:
            raise ValueError("security_code cannot be empty")
        start_time = self._normalize_time(start) if start is not None else None
        end_time = self._normalize_time(end) if end is not None else None
        if start_time is not None and end_time is not None and start_time > end_time:
            raise ValueError("start must not be after end")

        found: dict[datetime, tuple[Decimal, set[str]]] = {}
        for relative_path in self._candidate_files(code, start_time, end_time):
            path = self.root / Path(relative_path)
            try:
                table = pq.read_table(path, columns=["time", "thscode", "close"])
            except (OSError, pa.ArrowException) as error:
                raise MinuteCloseDataError(f"Cannot read Parquet file {path}: {error}") from error

            mask = pc.equal(table["thscode"], pa.scalar(code, type=table.schema.field("thscode").type))
            time_type = table.schema.field("time").type
            if start_time is not None:
                mask = pc.and_(mask, pc.greater_equal(table["time"], pa.scalar(start_time, type=time_type)))
            if end_time is not None:
                mask = pc.and_(mask, pc.less_equal(table["time"], pa.scalar(end_time, type=time_type)))
            rows = table.filter(mask).select(["time", "close"]).to_pylist()
            for row in rows:
                timestamp = row["time"]
                if timestamp is None:
                    raise MinuteCloseDataError(f"Missing time for {code} in {path}")
                close = self._decimal_close(row["close"], path=path, code=code, time=timestamp)
                current = found.get(timestamp)
                if current is None:
                    found[timestamp] = (close, {relative_path})
                elif current[0] != close:
                    previous_files = ", ".join(sorted(current[1]))
                    raise MinuteCloseConflictError(
                        f"Conflicting close for {code} at {timestamp}: {current[0]} in "
                        f"[{previous_files}] vs {close} in [{relative_path}]"
                    )
                else:
                    current[1].add(relative_path)

        bars = tuple(
            MinuteCloseBar(
                time=timestamp,
                security_code=code,
                close=close,
                source_files=tuple(sorted(paths)),
            )
            for timestamp, (close, paths) in sorted(found.items())
        )
        if continuous_auction_only:
            # Apply the trading-session gate after reading source timestamps.
            # Their timestamp[us] values are already Beijing-local (no tz).
            from etf_arb.rules.a_share import is_continuous_auction_time

            return tuple(bar for bar in bars if is_continuous_auction_time(bar.time))
        return bars

    def get_at_or_before(self, security_code: str, time: datetime) -> MinuteCloseBar | None:
        """Return the latest close no later than naive Beijing-local ``time``.

        Search newest monthly partition first and stop when data is found, so a
        quote lookup near the end of the dataset does not load all prior years.
        """
        code = str(security_code).strip()
        if not code:
            raise ValueError("security_code cannot be empty")
        end_time = self._normalize_time(time)
        candidates = self._candidate_files(code, None, end_time)
        months = sorted(
            {Path(relative_path).parent.name for relative_path in candidates},
            reverse=True,
        )
        for month in months:
            year, month_number = (int(part) for part in month.split("-"))
            month_start = datetime(year, month_number, 1)
            if month_number == 12:
                next_month_start = datetime(year + 1, 1, 1)
            else:
                next_month_start = datetime(year, month_number + 1, 1)
            month_end = min(end_time, next_month_start - timedelta(microseconds=1))
            bars = self.get_closes(code, start=month_start, end=month_end)
            if bars:
                return bars[-1]
        return None


__all__ = [
    "MinuteCloseBar",
    "MinuteCloseConflictError",
    "MinuteCloseDataError",
    "MinuteCloseScanReport",
    "ParquetMinuteCloseRepository",
]
