"""Point-in-time security checks shared by signals and simulated orders."""

from etf_arb.rules.errors import RuleNotFoundError


def security_trade_block(repo, symbol, at, side, policy):
    rule = None
    for code in (symbol, symbol.split(".")[0]):
        try:
            rule = repo.get_security_rule(code, at.date())
            break
        except RuleNotFoundError:
            # A gap in supplied history is never permission to assume tradability.
            if getattr(repo, "security_rules", lambda c: ())(code):
                return "missing_dated_security_rule"
    if rule is None:
        return "missing_security_rule" if policy == "require" else ""
    if not rule.is_trading_time(at.time()) or not (
        rule.can_buy() if side == "BUY" else rule.can_sell()
    ):
        return "security_rule_blocked"
    return ""
