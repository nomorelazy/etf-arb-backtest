"""报告输入 CSV 读取器。"""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from etf_arb.report.models import ClosedTrade, EquityPoint


def _rows(path: str | Path) -> tuple[list[str], list[dict[str, str]]]:
    with Path(path).open("r", newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return reader.fieldnames, list(reader)


def _decimal(value: str, field: str, path: str | Path) -> Decimal:
    try:
        return Decimal(value.strip())
    except (InvalidOperation, AttributeError) as error:
        raise ValueError(f"Invalid {field} value in {path}: {value!r}") from error


def load_equity_csv(path: str | Path) -> list[EquityPoint]:
    fields, rows = _rows(path)
    if not {"date", "equity"}.issubset(fields):
        raise ValueError("equity CSV must contain columns: date,equity")
    result: list[EquityPoint] = []
    for row in rows:
        try:
            day = date.fromisoformat(row["date"].strip())
        except (ValueError, AttributeError) as error:
            raise ValueError(f"Invalid date in {path}: {row.get('date')!r}") from error
        result.append(EquityPoint(day, _decimal(row.get("equity", ""), "equity", path)))
    return result


def load_trades_csv(path: str | Path) -> list[ClosedTrade]:
    fields, rows = _rows(path)
    if not {"trade_id", "net_pnl"}.issubset(fields):
        raise ValueError("trades CSV must contain columns: trade_id,net_pnl")
    return [
        ClosedTrade(
            trade_id=row.get("trade_id", "").strip(),
            pnl=_decimal(row.get("net_pnl", ""), "net_pnl", path),
        )
        for row in rows
    ]
