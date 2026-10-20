"""Read-only PCF/minute-data completeness checks; never fill or repair data.

Without an explicit trading calendar, dates are the union of observed PCF and
market dates. Days absent from both sources cannot be detected in that mode.
The default minute grid describes this project's iFinD source labels, including
11:30 and 15:00; it is not a definition of executable trading sessions.
"""

from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import date, datetime, time
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from etf_arb.data.pcf import FilePCFRepository, PCFParseError
from etf_arb.domain import PCFComponent

_MANDATORY_FLAGS = {"2", "必须", "必须现金替代", "深市必须", "沪市必须"}


def ifind_minute_grid() -> tuple[time, ...]:
    """242 source timestamps: 09:30..11:30 and 13:00..15:00, inclusive."""
    return tuple(
        time(minute // 60, minute % 60)
        for start, end in ((570, 690), (780, 900))
        for minute in range(start, end + 1)
    )


@dataclass(frozen=True)
class MissingIssue:
    kind: str
    status: str = "MISSING"  # UNCHECKED means coverage could not be determined.
    etf_code: str = ""
    day: str = ""
    security_code: str = ""
    field: str = ""
    source: str = ""
    missing_count: int = 0
    missing_times: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class MissingReport:
    start_date: date
    end_date: date
    etf_codes: tuple[str, ...]
    calendar_source: str
    checked_dates: tuple[date, ...]
    expected_times: tuple[time, ...]
    pcf_checks: int
    security_day_checks: int
    skipped_component_days: int
    issues: tuple[MissingIssue, ...]

    def summary(self) -> dict[str, object]:
        unchecked = sum(issue.status == "UNCHECKED" for issue in self.issues)
        missing = sum(issue.status == "MISSING" for issue in self.issues)
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "etf_codes": self.etf_codes,
            "calendar_source": self.calendar_source,
            "checked_dates": [day.isoformat() for day in self.checked_dates],
            "expected_minutes_per_day": len(self.expected_times),
            "expected_times": [value.isoformat() for value in self.expected_times],
            "pcf_checks": self.pcf_checks,
            "security_day_checks": self.security_day_checks,
            "skipped_component_days": self.skipped_component_days,
            "missing_issue_count": missing,
            "missing_minute_count": sum(issue.missing_count for issue in self.issues),
            "unchecked_issue_count": unchecked,
            "issue_counts": dict(Counter(issue.kind for issue in self.issues)),
            "passed": (
                self.calendar_source == "explicit"
                and bool(self.checked_dates)
                and not self.issues
            ),
            "notes": [
                "Missing records do not establish suspension or tradability.",
                "No forward fill, backfill, price repair or anomaly detection is performed.",
                "A non-null close counts as present; price validity is a separate check.",
                "Observed dates cannot reveal a whole day absent from both data sources.",
                "Calendar dates must cover the selected ETF's active period.",
                "Counts are per ETF dependency; a shared missing security can be counted for several ETFs.",
            ],
        }

    def save(self, output: str | Path) -> tuple[Path, Path]:
        folder = Path(output)
        folder.mkdir(parents=True, exist_ok=True)
        summary_path, detail_path = folder / "summary.json", folder / "missing.csv"
        summary_path.write_text(
            json.dumps(self.summary(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        fields = tuple(MissingIssue.__dataclass_fields__)
        with detail_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for issue in self.issues:
                row = asdict(issue)
                row["missing_times"] = ";".join(issue.missing_times)
                writer.writerow(row)
        return summary_path, detail_path


def _etf_code(value: str) -> tuple[str, str]:
    text = str(value).strip().upper()
    code, _, suffix = text.partition(".")
    if not re.fullmatch(r"[0-9]{6}", code):
        raise ValueError(f"Invalid ETF code: {value}")
    market = "SZ" if code.startswith("159") else "SH" if code.startswith("5") else None
    if market is None or (suffix and suffix != market):
        raise ValueError(f"Unsupported ETF code or mismatched exchange: {value}")
    return code, f"{code}.{market}"


def _requires_price(component: PCFComponent) -> bool | None:
    if component.is_cash_placeholder:
        return False
    flag = (component.substitution_flag or "").strip()
    # Source labels are deliberately explicit; unknown flags are not guessed.
    if flag in _MANDATORY_FLAGS or flag.startswith("2-"):
        return False
    if component.quantity == 0:
        return False
    if flag in {
        "0", "1", "禁止", "禁止现金替代", "允许", "允许现金替代",
        "可以现金替代", "深市退补", "沪市退补",
    } or flag.startswith(("0-", "1-")):
        return True
    return None


def _component_symbol(component: PCFComponent, symbols: set[str]) -> str | None:
    code = component.security_code.strip().upper()
    if "." in code:
        return code
    market = {"101": "SH", "102": "SZ"}.get(component.market_code or "")
    if market:
        return f"{code}.{market}"
    matches = sorted(symbol for symbol in symbols if symbol.split(".")[0] == code)
    if len(matches) > 1:
        return None
    # An absent raw code remains a missing security; do not invent its exchange.
    return matches[0] if matches else code


def check_missing(
    *,
    pcf_root: str | Path,
    minute_root: str | Path,
    etf_codes: Iterable[str],
    start_date: date,
    end_date: date,
    trading_days: Iterable[date] | None = None,
    expected_times: Iterable[time] | None = None,
) -> MissingReport:
    """Check files, required cash/flag fields and exact minute-price presence.

    ``trading_days`` is an externally supplied reference, never weekday guesses.
    ``expected_times`` can override the source's 242 timestamp convention.
    Missing minutes mean no non-null close exists at that exact timestamp in
    readable files. Duplicates do not increase coverage. No older price is used.
    """
    if type(start_date) is not date or type(end_date) is not date:
        raise TypeError("start_date/end_date must be dates")
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    etfs = tuple(_etf_code(value) for value in etf_codes)
    if not etfs or len({code for code, _ in etfs}) != len(etfs):
        raise ValueError("Provide unique ETF codes")
    roots = Path(pcf_root), Path(minute_root)
    for root in roots:
        if not root.is_dir():
            raise FileNotFoundError(f"Data root does not exist: {root}")
    clocks = tuple(ifind_minute_grid() if expected_times is None else expected_times)
    if not clocks or any(not isinstance(t, time) or t.tzinfo is not None for t in clocks):
        raise ValueError("expected_times must contain naive time values")
    if tuple(sorted(set(clocks))) != clocks:
        raise ValueError("expected_times must be strictly increasing and unique")
    explicit_days = None if trading_days is None else tuple(trading_days)
    if explicit_days is not None:
        if any(type(day) is not date for day in explicit_days):
            raise TypeError("trading_days must contain date values")
        if len(set(explicit_days)) != len(explicit_days):
            raise ValueError("trading_days contains duplicate dates")
        explicit_days = tuple(sorted(d for d in explicit_days if start_date <= d <= end_date))
        if not explicit_days:
            raise ValueError("No supplied trading days in the requested interval")

    issues: list[MissingIssue] = []
    observed_days: set[date] = set()
    symbols: set[str] = set()
    presence: dict[tuple[date, str], int] = defaultdict(int)
    clock_bits = {clock: 1 << i for i, clock in enumerate(clocks)}
    # Only source month folders are scanned. No SQLite catalog or source file is changed.
    files = sorted(
        path
        for folder in roots[1].iterdir()
        if folder.is_dir() and re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", folder.name)
        and start_date.strftime("%Y-%m") <= folder.name <= end_date.strftime("%Y-%m")
        for path in folder.iterdir()
        if path.is_file() and path.suffix.lower() == ".parquet"
    )
    for path in files:
        try:
            parquet = pq.ParquetFile(path)
            missing_columns = {"time", "thscode", "close"} - set(parquet.schema_arrow.names)
            if missing_columns:
                for column in sorted(missing_columns):
                    issues.append(MissingIssue("MISSING_COLUMN", source=str(path), field=column))
                issues.append(MissingIssue(
                    "MARKET_FILE_UNCHECKED", status="UNCHECKED", source=str(path),
                    detail="Missing columns prevent checking this file's minute coverage.",
                ))
                continue
            dtype = parquet.schema_arrow.field("time").type
            if not pa.types.is_timestamp(dtype) or dtype.tz is not None:
                raise ValueError("time must be a timezone-naive timestamp")
            code_type = parquet.schema_arrow.field("thscode").type
            if not (pa.types.is_string(code_type) or pa.types.is_large_string(code_type)):
                raise ValueError("thscode must be a string to identify securities")
            for batch in parquet.iter_batches(columns=["time", "thscode", "close"], batch_size=131072):
                table = pa.Table.from_batches([batch])
                mask = pc.and_(
                    pc.greater_equal(table["time"], pa.scalar(datetime.combine(start_date, time.min), type=dtype)),
                    pc.less_equal(table["time"], pa.scalar(datetime.combine(end_date, time.max), type=dtype)),
                )
                table = table.filter(mask)
                # Preserve date/code even when close is null, to distinguish missing prices.
                # Many securities share timestamps, and each code repeats for
                # hundreds of rows. Decode dictionaries once per batch rather
                # than constructing a Python datetime/string for every row.
                encoded_times = pc.dictionary_encode(table["time"].combine_chunks())
                encoded_codes = pc.dictionary_encode(table["thscode"].combine_chunks())
                time_keys = [
                    (stamp.date(), clock_bits.get(stamp.time(), 0))
                    for stamp in encoded_times.dictionary.to_pylist()
                ]
                code_keys = encoded_codes.dictionary.to_pylist()
                observed_days.update(day for day, _ in time_keys)
                symbols.update(code_keys)
                time_indices = encoded_times.indices.to_pylist()
                code_indices = encoded_codes.indices.to_pylist()
                present = pc.is_valid(table["close"]).to_pylist()
                for time_index, code_index, has_close in zip(time_indices, code_indices, present):
                    if code_index is None:
                        continue
                    day, bit = time_keys[time_index]
                    if has_close and bit:
                        presence[day, code_keys[code_index]] |= bit
                null_times = batch.column(batch.schema.get_field_index("time")).null_count
                if null_times:
                    issues.append(MissingIssue(
                        "NULL_TIME", status="UNCHECKED", source=str(path),
                        detail=f"{null_times} rows cannot be assigned to a trading date.",
                    ))
                null_codes = table["thscode"].null_count
                if null_codes:
                    issues.append(MissingIssue(
                        "NULL_SECURITY_CODE", status="UNCHECKED", source=str(path),
                        detail=f"{null_codes} in-range rows cannot be assigned to a security.",
                    ))
        except (OSError, pa.ArrowException, ValueError, TypeError) as error:
            issues.append(MissingIssue(
                "MARKET_FILE_UNCHECKED", status="UNCHECKED", source=str(path), detail=str(error),
            ))

    if explicit_days is None:
        for code, _ in etfs:
            folder = roots[0] / code
            if folder.is_dir():
                for path in folder.iterdir():
                    if not path.is_file() or not re.fullmatch(r"[0-9]{8}\.(xml|json)", path.name):
                        continue
                    try:
                        day = datetime.strptime(path.stem, "%Y%m%d").date()
                    except ValueError:
                        continue
                    if start_date <= day <= end_date:
                        observed_days.add(day)
        days = tuple(sorted(observed_days))
    else:
        days = explicit_days
    if not days:
        issues.append(MissingIssue(
            "NO_REFERENCE_DATES", status="UNCHECKED",
            detail="No observed dates. Supply trading_days to detect days absent from both sources.",
        ))

    repository = FilePCFRepository(roots[0])
    all_bits = (1 << len(clocks)) - 1
    security_checks = skipped_days = 0

    def check_security(etf: str, day: date, symbol: str, role: str) -> None:
        nonlocal security_checks
        security_checks += 1
        found = presence.get((day, symbol), 0)
        if found == all_bits:
            return
        missing = tuple(clock.isoformat() for clock, bit in clock_bits.items() if not found & bit)
        issues.append(MissingIssue(
            kind=f"MISSING_{role}_DAY" if not found else f"MISSING_{role}_MINUTES",
            etf_code=etf, day=day.isoformat(), security_code=symbol,
            missing_count=len(missing), missing_times=missing,
            detail="No non-null close at these exact source timestamps; cause not inferred.",
        ))

    for code, etf_symbol in etfs:
        for day in days:
            check_security(code, day, etf_symbol, "ETF")
            try:
                pcf = repository.get(code, day)
            except FileNotFoundError:
                skipped_days += 1
                issues.append(MissingIssue(
                    "MISSING_PCF", etf_code=code, day=day.isoformat(),
                    source=str(roots[0] / code / day.strftime("%Y%m%d")),
                    detail="Component checks skipped because the required PCF is absent.",
                ))
                continue
            except (PCFParseError, OSError, ValueError) as error:
                skipped_days += 1
                issues.append(MissingIssue(
                    "PCF_UNCHECKED", status="UNCHECKED", etf_code=code,
                    day=day.isoformat(), detail=str(error),
                ))
                continue
            # SZ RecordNum is not the total component count; only TotalRecordNum
            # can be used there. SSE RecordNumber describes the full list.
            expected_rows = pcf.total_record_num
            if expected_rows is None and pcf.exchange == "SH":
                expected_rows = pcf.record_num
            if expected_rows is not None and len(pcf.components) < expected_rows:
                issues.append(MissingIssue(
                    "MISSING_PCF_COMPONENT_ROWS", etf_code=code, day=day.isoformat(),
                    field="components", source=pcf.source_path or "",
                    detail=f"Source declares {expected_rows} rows; parsed {len(pcf.components)}.",
                ))
            if pcf.estimated_cash_component is None:
                issues.append(MissingIssue(
                    "MISSING_PCF_FIELD", etf_code=code, day=day.isoformat(),
                    field="estimated_cash_component", source=pcf.source_path or "",
                ))
            checked_symbols: set[str] = set()
            for component in pcf.components:
                if component.is_cash_placeholder:
                    continue
                required = _requires_price(component)
                if not component.substitution_flag:
                    issues.append(MissingIssue(
                        "MISSING_PCF_FIELD", etf_code=code, day=day.isoformat(),
                        security_code=component.security_code, field="substitution_flag",
                        source=pcf.source_path or "",
                    ))
                    issues.append(MissingIssue(
                        "COMPONENT_REQUIREMENT_UNCHECKED", status="UNCHECKED",
                        etf_code=code, day=day.isoformat(), security_code=component.security_code,
                        detail="Cannot classify component price requirements without a substitution flag.",
                    ))
                    continue
                if required is None:
                    issues.append(MissingIssue(
                        "COMPONENT_REQUIREMENT_UNCHECKED", status="UNCHECKED",
                        etf_code=code, day=day.isoformat(), security_code=component.security_code,
                        detail=f"Unsupported substitution flag: {component.substitution_flag}",
                    ))
                    continue
                flag = component.substitution_flag.strip()
                mandatory = flag in _MANDATORY_FLAGS or flag.startswith("2-")
                if mandatory:
                    cash_fields = (
                        ("creation_cash_substitute", "redemption_cash_substitute")
                        if pcf.exchange == "SZ" else ("substitution_cash_amount",)
                    )
                    for field in cash_fields:
                        if getattr(component, field) is None:
                            issues.append(MissingIssue(
                                "MISSING_PCF_FIELD", etf_code=code, day=day.isoformat(),
                                security_code=component.security_code, field=field,
                                source=pcf.source_path or "",
                            ))
                if not required:
                    continue
                symbol = _component_symbol(component, symbols)
                if symbol is None:
                    issues.append(MissingIssue(
                        "AMBIGUOUS_SECURITY", status="UNCHECKED", etf_code=code,
                        day=day.isoformat(), security_code=component.security_code,
                        detail="Multiple exchanges match the raw code; no market mapping was guessed.",
                    ))
                elif symbol not in checked_symbols:
                    checked_symbols.add(symbol)
                    check_security(code, day, symbol, "COMPONENT")

    return MissingReport(
        start_date=start_date, end_date=end_date,
        etf_codes=tuple(code for code, _ in etfs),
        calendar_source="observed" if explicit_days is None else "explicit",
        checked_dates=days, expected_times=clocks, pcf_checks=len(etfs) * len(days),
        security_day_checks=security_checks, skipped_component_days=skipped_days,
        issues=tuple(issues),
    )


__all__ = ["MissingIssue", "MissingReport", "check_missing", "ifind_minute_grid"]
