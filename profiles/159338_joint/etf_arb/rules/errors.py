"""Errors raised while loading or resolving historical trading rules."""

from __future__ import annotations


class RuleError(ValueError):
    """Base class for invalid or unavailable rule data."""


class RuleOverlapError(RuleError):
    """Two versions for one instrument cover the same effective date."""


class RuleNotFoundError(LookupError, RuleError):
    """No rule version covers the requested instrument and date."""


__all__ = ["RuleError", "RuleNotFoundError", "RuleOverlapError"]
