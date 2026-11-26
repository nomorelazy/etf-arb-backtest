"""与具体文件格式无关的数据校验。"""

from __future__ import annotations

from dataclasses import dataclass

from etf_arb.domain import PCF, Quote


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    severity: str = "ERROR"


def validate_pcf(pcf: PCF) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    codes = [component.security_code for component in pcf.components]
    if len(codes) != len(set(codes)):
        issues.append(
            ValidationIssue(
                "DUPLICATE_COMPONENT",
                "PCF contains repeated security codes; aggregate or verify them against the source.",
            )
        )
    if not any(component.quantity > 0 for component in pcf.components):
        issues.append(ValidationIssue("EMPTY_BASKET", "PCF has no positive component quantities."))
    return tuple(issues)


def validate_quote(quote: Quote) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    for field_name, value in (("bid", quote.bid), ("ask", quote.ask), ("last", quote.last)):
        if value is not None and value <= 0:
            issues.append(ValidationIssue("NON_POSITIVE_PRICE", f"{field_name} must be positive."))
    if quote.bid is not None and quote.ask is not None and quote.bid > quote.ask:
        issues.append(ValidationIssue("CROSSED_MARKET", "bid is greater than ask."))
    if quote.volume is not None and quote.volume < 0:
        issues.append(ValidationIssue("NEGATIVE_VOLUME", "volume cannot be negative."))
    return tuple(issues)
