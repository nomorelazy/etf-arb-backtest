"""ETF application and underlying-security trading rules."""

from etf_arb.rules.a_share import (
    AShareBoard,
    CONTINUOUS_AUCTION_SESSIONS,
    DELISTING_MAX_SUSPENDED_TRADING_DAYS,
    DELISTING_PERIOD_TRADING_DAYS,
    delisting_trading_day_index,
    MAIN_ST_TEN_PERCENT_FROM,
    MAIN_ST_PRICE_LIMIT_HISTORY,
    a_share_price_limit_rate,
    is_a_share_rule_trading_time,
    is_continuous_auction_time,
    make_a_share_security_rule,
)
from etf_arb.rules.errors import RuleError, RuleNotFoundError, RuleOverlapError
from etf_arb.rules.models import ETFRule, RuleRepository, SecurityTradingRule
from etf_arb.rules.repository import (
    HistoricalRuleRepository,
    InMemoryRuleRepository,
    RuleBook,
)

__all__ = [
    "AShareBoard",
    "CONTINUOUS_AUCTION_SESSIONS",
    "DELISTING_MAX_SUSPENDED_TRADING_DAYS",
    "DELISTING_PERIOD_TRADING_DAYS",
    "delisting_trading_day_index",
    "MAIN_ST_TEN_PERCENT_FROM",
    "MAIN_ST_PRICE_LIMIT_HISTORY",
    "a_share_price_limit_rate",
    "is_a_share_rule_trading_time",
    "is_continuous_auction_time",
    "make_a_share_security_rule",
    "ETFRule",
    "HistoricalRuleRepository",
    "InMemoryRuleRepository",
    "RuleBook",
    "RuleError",
    "RuleNotFoundError",
    "RuleOverlapError",
    "RuleRepository",
    "SecurityTradingRule",
]
