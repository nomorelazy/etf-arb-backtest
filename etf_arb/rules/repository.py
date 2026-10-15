"""In-memory historical rule repository used by the backtest engine."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from etf_arb.rules.errors import RuleNotFoundError, RuleOverlapError
from etf_arb.rules.models import ETFRule, SecurityTradingRule


def _intervals_overlap(
    left_from: date,
    left_to: date | None,
    right_from: date,
    right_to: date | None,
) -> bool:
    left_end = left_to or date.max
    right_end = right_to or date.max
    return left_from <= right_end and right_from <= left_end


class InMemoryRuleRepository:
    """Store immutable, effective-dated rule versions with no silent fallback.

    Rules for an instrument must have disjoint inclusive date intervals. An
    open-ended version is therefore only valid as the final version for that
    instrument.
    """

    def __init__(
        self,
        etf_rules: Iterable[ETFRule] = (),
        security_rules: Iterable[SecurityTradingRule] = (),
    ) -> None:
        self._etf_rules: dict[str, list[ETFRule]] = {}
        self._security_rules: dict[str, list[SecurityTradingRule]] = {}
        for rule in etf_rules:
            self.add_etf_rule(rule)
        for rule in security_rules:
            self.add_security_rule(rule)

    def add(self, rule: ETFRule | SecurityTradingRule) -> None:
        if isinstance(rule, ETFRule):
            self.add_etf_rule(rule)
        elif isinstance(rule, SecurityTradingRule):
            self.add_security_rule(rule)
        else:
            raise TypeError("rule must be ETFRule or SecurityTradingRule.")

    def add_etf_rule(self, rule: ETFRule) -> None:
        versions = self._etf_rules.setdefault(rule.etf_code, [])
        self._check_overlap(versions, rule, rule.etf_code)
        versions.append(rule)
        versions.sort(key=lambda item: item.effective_from)

    def add_security_rule(self, rule: SecurityTradingRule) -> None:
        versions = self._security_rules.setdefault(rule.security_code, [])
        self._check_overlap(versions, rule, rule.security_code)
        versions.append(rule)
        versions.sort(key=lambda item: item.effective_from)

    @staticmethod
    def _check_overlap(versions: list[object], candidate: object, code: str) -> None:
        for existing in versions:
            if _intervals_overlap(
                existing.effective_from,
                existing.effective_to,
                candidate.effective_from,
                candidate.effective_to,
            ):
                raise RuleOverlapError(
                    f"Overlapping rule versions for {code}: "
                    f"{existing.effective_from}..{existing.effective_to} and "
                    f"{candidate.effective_from}..{candidate.effective_to}."
                )

    @staticmethod
    def _validate_day(day: date) -> None:
        if type(day) is not date:
            raise TypeError("day must be a date.")

    def get(self, etf_code: str, day: date) -> ETFRule:
        self._validate_day(day)
        code = str(etf_code).strip()
        rule = next(
            (candidate for candidate in self._etf_rules.get(code, ()) if candidate.applies_on(day)),
            None,
        )
        if rule is None:
            raise RuleNotFoundError(f"No ETF rule for {code} on {day}.")
        return rule

    def get_security_rule(self, security_code: str, day: date) -> SecurityTradingRule:
        self._validate_day(day)
        code = str(security_code).strip()
        rule = next(
            (
                candidate
                for candidate in self._security_rules.get(code, ())
                if candidate.applies_on(day)
            ),
            None,
        )
        if rule is None:
            raise RuleNotFoundError(f"No security trading rule for {code} on {day}.")
        return rule

    def etf_rules(self, etf_code: str) -> tuple[ETFRule, ...]:
        return tuple(self._etf_rules.get(str(etf_code).strip(), ()))

    def security_rules(self, security_code: str) -> tuple[SecurityTradingRule, ...]:
        return tuple(self._security_rules.get(str(security_code).strip(), ()))


HistoricalRuleRepository = InMemoryRuleRepository
RuleBook = InMemoryRuleRepository


__all__ = ["HistoricalRuleRepository", "InMemoryRuleRepository", "RuleBook"]
