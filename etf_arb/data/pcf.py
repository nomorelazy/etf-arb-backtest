"""官方申赎清单的解析和本地文件仓库。

原始 XML/JSON 文件保持不变；解析器把深交所 XML、上交所 XML 和旧版
JSON 映射到统一的 :class:`~etf_arb.domain.PCF`。所有数值先从文本构造
``Decimal``，证券代码保持字符串，避免前导零和浮点误差。
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from etf_arb.domain import PCF, PCFComponent


class PCFParseError(ValueError):
    """PCF 文件格式、字段或业务数据无法解析。"""


class PCFRepository(Protocol):
    def get(self, etf_code: str, effective_date: date) -> PCF:
        """返回指定 ETF 和日期适用的 PCF；无记录时应明确报错。"""
        ...


_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(value: object, *, field: str, required: bool = False) -> str | None:
    if value is None:
        if required:
            raise PCFParseError(f"Missing required field: {field}")
        return None
    result = str(value).strip()
    if not result or result in {"-", "--", "null", "None"}:
        if required:
            raise PCFParseError(f"Missing required field: {field}")
        return None
    return result


def _decimal(value: object, *, field: str, required: bool = False) -> Decimal | None:
    text = _text(value, field=field, required=required)
    if text is None:
        return None
    # Some old JSON files store percentages as "10.000%".
    text = text.removesuffix("%").strip()
    try:
        result = Decimal(text)
    except InvalidOperation as error:
        raise PCFParseError(f"Invalid decimal in {field}: {value!r}") from error
    if not result.is_finite():
        raise PCFParseError(f"Non-finite decimal in {field}: {value!r}")
    return result


def _rate(value: object, *, field: str) -> Decimal | None:
    """Parse a source rate into a decimal ratio (10.000% -> 0.10)."""
    text = _text(value, field=field)
    if text is None:
        return None
    has_percent_suffix = text.endswith("%")
    result = _decimal(text.removesuffix("%"), field=field)
    if result is None:
        return None
    return result / Decimal(100) if has_percent_suffix else result


def _integer(value: object, *, field: str, required: bool = False) -> int | None:
    result = _decimal(value, field=field, required=required)
    if result is None:
        return None
    if result != result.to_integral_value():
        raise PCFParseError(f"Expected integer in {field}: {value!r}")
    return int(result)


def _required_decimal(value: object, *, field: str) -> Decimal:
    result = _decimal(value, field=field, required=True)
    assert result is not None
    return result


def _required_integer(value: object, *, field: str) -> int:
    result = _integer(value, field=field, required=True)
    assert result is not None
    return result


def _date(value: object, *, field: str, required: bool = False) -> date | None:
    text = _text(value, field=field, required=required)
    if text is None:
        return None
    match = _DATE_RE.fullmatch(text[:8])
    if not match:
        raise PCFParseError(f"Expected YYYYMMDD date in {field}: {value!r}")
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError as error:
        raise PCFParseError(f"Invalid date in {field}: {value!r}") from error


def _yn(value: object, *, field: str) -> bool | None:
    text = _text(value, field=field)
    if text is None:
        return None
    if text.upper() in {"Y", "YES", "1", "TRUE"}:
        return True
    if text.upper() in {"N", "NO", "0", "FALSE"}:
        return False
    raise PCFParseError(f"Invalid boolean flag in {field}: {value!r}")


def _exchange_from_etf_code(etf_code: str) -> str:
    if not re.fullmatch(r"\d{6}", etf_code):
        raise PCFParseError(f"ETF code must be six digits: {etf_code!r}")
    if etf_code.startswith("5"):
        return "SH"
    if etf_code.startswith(("1", "0", "3")):
        return "SZ"
    raise PCFParseError(f"Cannot infer exchange from ETF code: {etf_code}")


def _normalized_source_fields(fields: dict[str, object]) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (str(key), str(value).strip())
            for key, value in fields.items()
            if value is not None and str(value).strip() != ""
        )
    )


def _read_xml_fields(root: ET.Element) -> tuple[dict[str, str], list[dict[str, str]], str]:
    root_name = _local_name(root.tag)
    if root_name == "PCFFile":
        exchange = "SZ"
        component_container = "Components"
        fields = {
            _local_name(child.tag): (child.text or "").strip()
            for child in root
            if _local_name(child.tag) != component_container
        }
        components_parent = next(
            (child for child in root if _local_name(child.tag) == component_container), None
        )
        component_rows: list[dict[str, str]] = []
        if components_parent is not None:
            for node in components_parent:
                if _local_name(node.tag) != "Component":
                    continue
                component_rows.append(
                    {_local_name(child.tag): (child.text or "").strip() for child in node}
                )
        return fields, component_rows, exchange

    if root_name == "SSEPortfolioCompositionFile":
        exchange = "SH"
        component_container = "ComponentList"
        fields = {
            _local_name(child.tag): (child.text or "").strip()
            for child in root
            if _local_name(child.tag) != component_container
        }
        components_parent = next(
            (child for child in root if _local_name(child.tag) == component_container), None
        )
        component_rows = []
        if components_parent is not None:
            for node in components_parent:
                if _local_name(node.tag) != "Component":
                    continue
                component_rows.append(
                    {_local_name(child.tag): (child.text or "").strip() for child in node}
                )
        return fields, component_rows, exchange

    raise PCFParseError(f"Unsupported PCF XML root: {root_name}")


def _component_from_shenzhen(row: dict[str, str]) -> PCFComponent:
    code = _text(row.get("UnderlyingSecurityID"), field="Component.UnderlyingSecurityID", required=True)
    assert code is not None
    return PCFComponent(
        security_code=code,
        quantity=_required_decimal(row.get("ComponentShare"), field="Component.ComponentShare"),
        security_name=_text(row.get("UnderlyingSymbol"), field="Component.UnderlyingSymbol"),
        substitution_flag=_text(row.get("SubstituteFlag"), field="Component.SubstituteFlag"),
        creation_premium_rate=_rate(row.get("PremiumRatio"), field="Component.PremiumRatio"),
        redemption_discount_rate=_rate(row.get("DiscountRatio"), field="Component.DiscountRatio"),
        creation_cash_substitute=_decimal(
            row.get("CreationCashSubstitute"), field="Component.CreationCashSubstitute"
        ),
        redemption_cash_substitute=_decimal(
            row.get("RedemptionCashSubstitute"), field="Component.RedemptionCashSubstitute"
        ),
        market_code=_text(row.get("UnderlyingSecurityIDSource"), field="Component.UnderlyingSecurityIDSource"),
        is_cash_placeholder=(code == "申赎现金" or _text(row.get("UnderlyingSymbol"), field="name") == "申赎现金"),
        raw_fields=_normalized_source_fields(row),
    )


def _component_from_shanghai(row: dict[str, str]) -> PCFComponent:
    code = _text(row.get("InstrumentID"), field="Component.InstrumentID", required=True)
    assert code is not None
    return PCFComponent(
        security_code=code,
        quantity=_required_decimal(row.get("Quantity"), field="Component.Quantity"),
        security_name=_text(row.get("InstrumentName"), field="Component.InstrumentName"),
        substitution_flag=_text(row.get("SubstitutionFlag"), field="Component.SubstitutionFlag"),
        creation_premium_rate=_rate(
            row.get("CreationPremiumRate"), field="Component.CreationPremiumRate"
        ),
        redemption_discount_rate=_rate(
            row.get("RedemptionDiscountRate"), field="Component.RedemptionDiscountRate"
        ),
        substitution_cash_amount=_decimal(
            row.get("SubstitutionCashAmount"), field="Component.SubstitutionCashAmount"
        ),
        market_code=_text(row.get("UnderlyingSecurityID"), field="Component.UnderlyingSecurityID"),
        raw_fields=_normalized_source_fields(row),
    )


def _pcf_from_fields(
    *,
    fields: dict[str, object],
    rows: list[PCFComponent],
    exchange: str,
    source_format: str,
    source_path: Path,
) -> PCF:
    etf_code = _text(
        fields.get("SecurityID") if exchange == "SZ" else fields.get("FundInstrumentID"),
        field="ETF code",
        required=True,
    )
    trading_day = _date(fields.get("TradingDay"), field="TradingDay", required=True)
    assert etf_code is not None and trading_day is not None
    estimated_cash = _decimal(
        fields.get(
            "EstimateCashComponent"
            if exchange == "SZ" or source_format == "json"
            else "EstimatedCashComponent"
        ),
        field="estimated cash component",
    )
    actual_cash = _decimal(fields.get("CashComponent"), field="CashComponent") if exchange == "SZ" else None
    # Legacy SSE JSON labels the previous PCF's cash component as
    # CashComponent under t-1; align it with the newer PreCashComponent field.
    pre_cash_value = fields.get("PreCashComponent")
    if source_format == "json" and pre_cash_value is None:
        pre_cash_value = fields.get("CashComponent")
    pre_cash = _decimal(pre_cash_value, field="PreCashComponent") if exchange == "SH" else None
    components = tuple(rows)
    # The cash placeholder is a reporting row, not a security. Keep it in the
    # normalized PCF for auditability; pricing code can filter it explicitly.
    if not components:
        raise PCFParseError(f"No Component rows in {source_path}")
    return PCF(
        etf_code=etf_code,
        effective_date=trading_day,
        creation_unit=_required_integer(fields.get("CreationRedemptionUnit"), field="CreationRedemptionUnit"),
        components=components,
        # Keep venue source fields separate. Their trading/accounting meaning
        # is not silently collapsed into a normalized signed adjustment.
        creation_cash_adjustment=None,
        redemption_cash_adjustment=None,
        exchange=exchange,
        source_format=source_format,
        source_path=str(source_path),
        version=_text(fields.get("Version"), field="Version"),
        symbol=_text(fields.get("Symbol"), field="Symbol"),
        fund_manager=_text(fields.get("FundManagementCompany"), field="FundManagementCompany"),
        security_id_source=_text(fields.get("SecurityIDSource"), field="SecurityIDSource"),
        underlying_index_code=_text(fields.get("UnderlyingSecurityID"), field="UnderlyingSecurityID")
        if exchange == "SZ" else None,
        underlying_index_source=_text(fields.get("UnderlyingSecurityIDSource"), field="UnderlyingSecurityIDSource")
        if exchange == "SZ" else None,
        pre_trading_day=_date(fields.get("PreTradingDay"), field="PreTradingDay"),
        estimated_cash_component=estimated_cash,
        actual_cash_component=actual_cash,
        pre_cash_component=pre_cash,
        max_cash_ratio=_rate(fields.get("MaxCashRatio"), field="MaxCashRatio"),
        nav=_decimal(fields.get("NAV"), field="NAV"),
        nav_per_cu=_decimal(fields.get("NAVperCU"), field="NAVperCU"),
        publish_flag=_text(
            fields.get("Publish" if exchange == "SZ" or source_format == "json" else "PublishIOPVFlag"),
            field="Publish",
        ),
        creation_enabled=_yn(fields.get("Creation"), field="Creation") if exchange == "SZ" else None,
        redemption_enabled=_yn(fields.get("Redemption"), field="Redemption") if exchange == "SZ" else None,
        creation_redemption_switch=_text(
            fields.get("CreationRedemptionSwitch", fields.get("CreationRedemption")),
            field="CreationRedemptionSwitch",
        ),
        creation_redemption_mechanism=_text(fields.get("CreationRedemptionMechanism"), field="CreationRedemptionMechanism"),
        creation_limit=_decimal(fields.get("CreationLimit"), field="CreationLimit"),
        redemption_limit=_decimal(fields.get("RedemptionLimit"), field="RedemptionLimit"),
        creation_limit_per_user=_decimal(fields.get("CreationLimitPerUser"), field="CreationLimitPerUser"),
        redemption_limit_per_user=_decimal(fields.get("RedemptionLimitPerUser"), field="RedemptionLimitPerUser"),
        net_creation_limit=_decimal(fields.get("NetCreationLimit"), field="NetCreationLimit"),
        net_redemption_limit=_decimal(fields.get("NetRedemptionLimit"), field="NetRedemptionLimit"),
        net_creation_limit_per_user=_decimal(fields.get("NetCreationLimitPerUser"), field="NetCreationLimitPerUser"),
        net_redemption_limit_per_user=_decimal(fields.get("NetRedemptionLimitPerUser"), field="NetRedemptionLimitPerUser"),
        record_num=_integer(fields.get("RecordNum" if exchange == "SZ" else "RecordNumber"), field="RecordNum"),
        total_record_num=_integer(fields.get("TotalRecordNum"), field="TotalRecordNum"),
        dividend_per_cu=_decimal(fields.get("DividendPerCU"), field="DividendPerCU"),
        raw_fields=_normalized_source_fields(fields),
    )


def parse_pcf_xml(path: str | Path) -> PCF:
    source_path = Path(path)
    try:
        root = ET.parse(source_path).getroot()
    except (OSError, ET.ParseError) as error:
        raise PCFParseError(f"Cannot read PCF XML: {source_path}") from error
    fields, rows, exchange = _read_xml_fields(root)
    parser = _component_from_shenzhen if exchange == "SZ" else _component_from_shanghai
    return _pcf_from_fields(
        fields=fields,
        rows=[parser(row) for row in rows],
        exchange=exchange,
        source_format="xml",
        source_path=source_path,
    )


def _json_properties(container: object) -> dict[str, object]:
    if not isinstance(container, dict):
        return {}
    result: dict[str, object] = {}
    for item in container.get("properties", []):
        if isinstance(item, list) and len(item) >= 3:
            result[str(item[2])] = item[1]
    return result


def _component_from_legacy_shanghai_json(row: list[object]) -> PCFComponent:
    code = _text(row[0], field="securities.code", required=True)
    assert code is not None
    raw_fields = {
        "InstrumentID": str(row[0]),
        "InstrumentName": str(row[1]),
        "Quantity": str(row[2]),
        "SubstitutionFlag": str(row[3]),
        "SubstitutionRate": str(row[4]),
        "SubstitutionCashAmount": str(row[5]),
    }
    return PCFComponent(
        security_code=code,
        quantity=_required_decimal(row[2], field="securities.quantity"),
        security_name=_text(row[1], field="securities.name"),
        substitution_flag=_text(row[3], field="securities.substitution_flag"),
        substitution_rate=_rate(row[4], field="securities.substitution_rate"),
        substitution_cash_amount=_decimal(row[5], field="securities.substitution_cash_amount"),
        raw_fields=_normalized_source_fields(raw_fields),
    )


def parse_pcf_json(path: str | Path) -> PCF:
    source_path = Path(path)
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise PCFParseError(f"Cannot read PCF JSON: {source_path}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("securities"), dict):
        raise PCFParseError(f"Unsupported PCF JSON shape: {source_path}")
    info = payload.get("info", [])
    info_map = {str(row[2]): row[1] for row in info if isinstance(row, list) and len(row) >= 3}
    current = _json_properties(payload.get("t"))
    previous = _json_properties(payload.get("t-1"))
    fields: dict[str, object] = {**previous, **current}
    # Legacy files can expose a secondary market identifier (e.g. Fundid1)
    # that differs from the six-digit ETF directory. Prefer the repository's
    # canonical directory code when available.
    directory_code = source_path.parent.name
    json_etf_code = directory_code if re.fullmatch(r"\d{6}", directory_code) else None
    fields["FundInstrumentID"] = json_etf_code or info_map.get("FundInstrumentID") or info_map.get("Fundid1")
    fields["TradingDay"] = info_map.get("TradingDay")
    rows_data = payload["securities"].get("rows", [])
    rows: list[PCFComponent] = []
    for row in rows_data:
        if not isinstance(row, list) or len(row) < 6:
            raise PCFParseError(f"Invalid securities row in {source_path}: {row!r}")
        rows.append(_component_from_legacy_shanghai_json(row))
    return _pcf_from_fields(
        fields=fields,
        rows=rows,
        exchange="SH",
        source_format="json",
        source_path=source_path,
    )


def parse_pcf(path: str | Path) -> PCF:
    source_path = Path(path)
    suffix = source_path.suffix.lower()
    if suffix == ".xml":
        return parse_pcf_xml(source_path)
    if suffix == ".json" and not source_path.name.startswith("_"):
        return parse_pcf_json(source_path)
    raise PCFParseError(f"Unsupported or non-data PCF file: {source_path.name}")


class FilePCFRepository:
    """按 ``<root>/<etf>/<YYYYMMDD>.(xml|json)`` 读取官方清单。"""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._cache: dict[tuple[str, date], PCF] = {}

    def clear_cache(self) -> None:
        """清除已解析清单；原始文件更新后可显式调用。"""
        self._cache.clear()

    def get(self, etf_code: str, effective_date: date) -> PCF:
        code = str(etf_code).strip()
        cache_key = (code, effective_date)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        day = effective_date.strftime("%Y%m%d")
        folder = self.root / code
        candidates = [folder / f"{day}.xml", folder / f"{day}.json"]
        for path in candidates:
            if path.is_file():
                pcf = parse_pcf(path)
                if pcf.etf_code != code:
                    raise PCFParseError(
                        f"ETF code mismatch: requested {code}, file contains {pcf.etf_code}: {path}"
                    )
                if pcf.effective_date != effective_date:
                    raise PCFParseError(
                        f"Trading date mismatch: requested {effective_date}, file contains {pcf.effective_date}: {path}"
                    )
                self._cache[cache_key] = pcf
                return pcf
        raise FileNotFoundError(f"No PCF file for {code} on {effective_date}: {folder}")


__all__ = [
    "FilePCFRepository",
    "PCFParseError",
    "PCFRepository",
    "parse_pcf",
    "parse_pcf_json",
    "parse_pcf_xml",
]
