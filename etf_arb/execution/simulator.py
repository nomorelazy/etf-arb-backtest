"""Zero-fee, zero-slippage minute-close research execution and settlement.

This is not an exchange matching engine. Prices/volume/status and manager
settlement assumptions are exported alongside every run.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from etf_arb.data.snapshot import component_symbol
from etf_arb.domain import Side
from etf_arb.execution.plan import application_allowed, build_plan
from etf_arb.portfolio.account import CashAccount, ZERO
from etf_arb.portfolio.ledger import LedgerEventType as Event
from etf_arb.report.models import EquityPoint
from etf_arb.rules import RuleNotFoundError, is_continuous_auction_time
from etf_arb.settlement.cash import CashLeg, trading_day_after


@dataclass
class SimOrder:
    order_id: str
    trade_id: str
    symbol: str
    side: str
    quantity: int
    submitted: datetime
    purpose: str
    not_before_day: date
    filled: int = 0
    value: Decimal = ZERO
    cancelled: bool = False
    last_block: str = ""

    @property
    def remaining(self):
        return 0 if self.cancelled else self.quantity - self.filled


@dataclass
class SimTrade:
    trade_id: str
    decision: object
    pcf: object
    plan: object
    state: str = "BUILD"
    applied_at: datetime | None = None
    cash_deposit: Decimal = ZERO
    legs: list = field(default_factory=list)
    reason: str = ""


class ArbitrageSimulator:
    def __init__(self, prices, settings):
        self.prices, self.settings = prices, settings
        self.account = None
        self.trades, self.orders, self.fills, self.rejections = [], [], [], []
        self.equity_curve, self.daily, self.marks = [], [], {}
        self.started_by_day = {}

    def available(self, day):
        return (not any(t.state != "CLOSED" for t in self.trades)
                and self.started_by_day.get(day, 0) < self.settings.max_trades_per_day)

    def _order(self, trade, symbol, side, quantity, now, purpose, not_before=None):
        if quantity:
            self.orders.append(SimOrder(f"O{len(self.orders)+1:07}", trade.trade_id, symbol, side,
                                        quantity, now, purpose, not_before or now.date()))

    def _orders(self, trade, purpose=None):
        return [o for o in self.orders if o.trade_id == trade.trade_id
                and (purpose is None or o.purpose == purpose)]

    def on_day_open(self, context):
        if self.account is None:
            self.account = CashAccount(context.resources.config.starting_cash, context.resources.portfolio)
            from datetime import timedelta
            self.equity_curve.append(EquityPoint(context.now.date() - timedelta(days=1), self.account.cash))
        symbols = {"159338.SZ"}
        for pcf in context.pcfs.values():
            symbols.update(component_symbol(c) for c in pcf.components if not c.is_cash_placeholder)
        for order in self.orders:
            if order.remaining:
                symbols.add(order.symbol)
        symbols.update(symbol for (_, symbol), q in self.account.positions.items() if q)
        self.prices.load(context.now.date(), symbols)
        for trade in self.trades:
            if trade.applied_at:
                for leg in trade.legs:
                    leg.observe_cash_component(
                        context.pcfs[trade.pcf.etf_code], trade.applied_at.date(), trade.decision.side,
                        trade.decision.quantity_units, trade.cash_deposit,
                        context.resources.config.trading_days, self.settings.cash_difference_lag,
                    )

    def _can_trade(self, order, context):
        now = context.now
        if now <= order.submitted or now.date() < order.not_before_day:
            return "waiting_for_later_bar_or_sell_availability"
        if not is_continuous_auction_time(now.time()):
            return "outside_continuous_auction"
        repo = context.resources.rules
        rule = None
        for code in (order.symbol, order.symbol.split(".")[0]):
            try:
                rule = repo.get_security_rule(code, now.date())
                break
            except RuleNotFoundError:
                # Even permissive research mode must not hide gaps in a supplied history.
                if getattr(repo, "security_rules", lambda c: ())(code):
                    return "missing_dated_security_rule"
        if rule is None:
            if self.settings.security_rule_policy == "require":
                return "missing_security_rule"
        elif not rule.is_trading_time(now.time()) or not (
            rule.can_buy() if order.side == "BUY" else rule.can_sell()
        ):
            return "security_rule_blocked"
        return ""

    def handle(self, decision, context):
        pcf = context.pcfs[decision.etf_code]
        reason = ""
        if not self.available(context.now.date()):
            reason = "active_trade_or_daily_limit"
        elif not is_continuous_auction_time(context.now.time()):
            reason = "outside_continuous_auction"
        elif not application_allowed(pcf, context.resources.rules.get(pcf.etf_code, context.now.date()),
                                     decision.side, decision.quantity_units, context.now.time()):
            reason = "application_disabled_or_limit"
        try:
            plan = build_plan(pcf, decision.side, decision.quantity_units)
        except ValueError as error:
            self.rejections.append(dict(ts=context.now, reason=str(error)))
            return
        entries = plan.physical if decision.side is Side.CREATE else {"159338.SZ": plan.etf_shares}
        needed = max(plan.estimated_cash, ZERO)
        if decision.side is Side.CREATE:
            needed += plan.substitution_deposit + plan.domestic_fixed
        for symbol, quantity in entries.items():
            bar = self.prices.get(symbol, context.now, exact=True)
            if bar is None:
                reason = "missing_entry_quote"
                break
            needed += quantity * bar.close
        if needed > self.account.cash:
            reason = "insufficient_cash"
        if reason:
            self.rejections.append(dict(ts=context.now, reason=reason))
            return
        trade = SimTrade(f"T{len(self.trades)+1:06}", decision, pcf, plan)
        self.trades.append(trade)
        self.started_by_day[context.now.date()] = self.started_by_day.get(context.now.date(), 0) + 1
        context.resources.portfolio.open_trade(trade_id=trade.trade_id, opened_at=context.now)
        for symbol, quantity in entries.items():
            self._order(trade, symbol, "BUY", quantity, context.now, "ENTRY")
        # An all-cash creation still waits for a later bar before applying.

    def _fill(self, order, trade, context):
        if not order.remaining:
            return
        block = self._can_trade(order, context)
        bar = self.prices.get(order.symbol, context.now, exact=True)
        if block or bar is None:
            order.last_block = block or "missing_exact_bar"
            return
        quantity = order.remaining
        cap = self.settings.max_shares_per_minute
        if cap is not None:
            quantity = min(quantity, cap)
        if order.purpose != "MANAGER":
            if order.side == "BUY":
                floor = max(trade.plan.estimated_cash, ZERO)
                if trade.decision.side is Side.CREATE:
                    floor += trade.plan.substitution_deposit + trade.plan.domestic_fixed
                affordable = max(0, int((self.account.cash - floor) / bar.close))
                quantity = min(quantity, affordable)
                quantity = quantity // 100 * 100
            elif quantity < order.remaining:
                quantity = quantity // 100 * 100
        if quantity <= 0:
            order.last_block = "insufficient_cash_or_lot_size"
            return
        if order.purpose != "MANAGER":
            self.account.fill(context.now, trade.trade_id, order.symbol, order.side, quantity, bar.close)
        order.filled += quantity
        order.value += quantity * bar.close
        order.last_block = ""
        self.marks[order.symbol] = bar.close
        self.fills.append(dict(ts=context.now, order_id=order.order_id, trade_id=trade.trade_id,
                               symbol=order.symbol, side=order.side, quantity=quantity, price=bar.close,
                               purpose=order.purpose, fee=ZERO, slippage=ZERO,
                               basis="manager_minute_proxy" if order.purpose == "MANAGER" else "next_minute_close"))

    def _apply(self, trade, context):
        plan, side, now = trade.plan, trade.decision.side, context.now
        if now <= trade.decision.ts or any(o.remaining for o in self._orders(trade, "ENTRY")):
            return
        rule = context.resources.rules.get(trade.pcf.etf_code, now.date())
        if now.date() != trade.pcf.effective_date or not application_allowed(
            trade.pcf, rule, side, trade.decision.quantity_units, now.time()
        ):
            self._unwind(trade, context, "application_cutoff_or_disabled")
            return
        deposit = max(plan.estimated_cash, ZERO)
        due_cash = deposit + (plan.substitution_deposit + plan.domestic_fixed if side is Side.CREATE else ZERO)
        if self.account.cash < due_cash:
            self._unwind(trade, context, "insufficient_application_cash")
            return
        trade.applied_at, trade.cash_deposit, trade.state = now, deposit, "APPLIED"
        self.account.post(now, trade.trade_id, Event.CREATE_SUBMITTED if side is Side.CREATE else Event.REDEEM_SUBMITTED,
                          cash=-due_cash)
        if side is Side.CREATE:
            for symbol, quantity in plan.physical.items():
                self.account.post(now, trade.trade_id, Event.APPLICATION_CONFIRMED, symbol=symbol, quantity=-quantity)
            self.account.post(now, trade.trade_id, Event.SECURITIES_AVAILABLE, symbol="159338.SZ", quantity=plan.etf_shares)
            self._order(trade, "159338.SZ", "SELL", plan.etf_shares, now, "EXIT")
            cash_estimate = deposit - plan.estimated_cash
            sub_estimate = plan.substitution_deposit - self._manager_value(trade, now)
        else:
            self.account.post(now, trade.trade_id, Event.APPLICATION_CONFIRMED, symbol="159338.SZ", quantity=-plan.etf_shares)
            for symbol, quantity in plan.physical.items():
                self.account.post(now, trade.trade_id, Event.SECURITIES_AVAILABLE, symbol=symbol, quantity=quantity)
                self._order(trade, symbol, "SELL", quantity, now, "EXIT")
            cash_estimate = deposit + plan.estimated_cash
            sub_estimate = self._manager_value(trade, now) + plan.domestic_fixed
        trade.legs = [CashLeg("cash_difference", cash_estimate), CashLeg("cash_substitution", sub_estimate)]
        for symbol, quantity in plan.manager.items():
            self._order(trade, symbol, "BUY" if side is Side.CREATE else "SELL", quantity, now, "MANAGER")
        # No invented manager fills: these orders will execute on later observable bars.

    def _manager_value(self, trade, now):
        result = trade.plan.manager_fixed
        orders = self._orders(trade, "MANAGER")
        filled_by_symbol = {o.symbol: o for o in orders}
        for symbol, required in trade.plan.manager.items():
            order = filled_by_symbol.get(symbol)
            filled = order.filled if order else 0
            result += order.value if order else ZERO
            if required > filled:
                bar = self.prices.get(symbol, now)
                if bar:
                    self.marks[symbol] = bar.close
                price = bar.close if bar else self.marks.get(symbol)
                if price is None:
                    raise ValueError(f"Cannot value pending cash substitution: {symbol}")
                result += (required - filled) * price
        return result

    def _settle(self, trade, context):
        now, plan = context.now, trade.plan
        sub = next(leg for leg in trade.legs if leg.kind == "cash_substitution")
        manager_value = self._manager_value(trade, now)
        sub.estimate = (plan.substitution_deposit - manager_value if trade.decision.side is Side.CREATE
                        else manager_value + plan.domestic_fixed)
        if sub.final is None and not any(o.remaining for o in self._orders(trade, "MANAGER")):
            sub.final, sub.known_on = sub.estimate, now.date()
            scheduled = trading_day_after(context.resources.config.trading_days, trade.applied_at.date(),
                                          self.settings.cash_substitution_lag)
            sub.due = None if scheduled is None else max(scheduled, now.date())
        for leg in trade.legs:
            if leg.paid or leg.final is None or leg.due is None or now.date() < leg.due:
                continue
            if self.account.cash + leg.final < 0:
                trade.reason = "insufficient_cash_for_settlement"
                continue
            self.account.post(now, trade.trade_id,
                              Event.CASH_SUBSTITUTION_SETTLED if leg.kind == "cash_substitution" else Event.CASH_SETTLED,
                              cash=leg.final)
            leg.paid = True

    def _unwind(self, trade, context, reason):
        trade.state, trade.reason = "UNWIND", reason
        for order in self._orders(trade, "ENTRY"):
            if order.remaining:
                order.cancelled = True
        # Stock purchases cannot be sold that same day. They are eligible for
        # creation transfer, but a failed application does not grant same-day sale.
        next_day = trading_day_after(context.resources.config.trading_days, trade.decision.ts.date(), 1)
        for symbol, quantity in self.account.holdings(trade.trade_id).items():
            eligible = next_day or date.max
            self._order(trade, symbol, "SELL", quantity, context.now, "UNWIND", eligible)
        self.account.post(context.now, trade.trade_id, Event.APPLICATION_REJECTED)

    def _close_if_done(self, trade, context):
        if trade.state == "BUILD" or self.account.holdings(trade.trade_id):
            return
        if any(o.remaining for o in self._orders(trade)) or any(not leg.paid for leg in trade.legs):
            return
        context.resources.portfolio.settle_trade(trade_id=trade.trade_id, settled_at=context.now,
                                                  pnl=self.account.trade_cash.get(trade.trade_id, ZERO))
        trade.state = "CLOSED"

    def on_time(self, context):
        if not is_continuous_auction_time(context.now.time()):
            return
        for trade in self.trades:
            if trade.state == "CLOSED":
                continue
            for order in self._orders(trade):
                self._fill(order, trade, context)
            if trade.state == "BUILD":
                self._apply(trade, context)
            if trade.state == "APPLIED":
                self._settle(trade, context)
            self._close_if_done(trade, context)

    def on_day_close(self, context):
        for trade in self.trades:
            if trade.state == "BUILD":
                self._unwind(trade, context, "basket_incomplete_at_close")
                self._close_if_done(trade, context)
        stale = []
        stale_substitution = []
        for trade in self.trades:
            if trade.state != "APPLIED":
                continue
            sub = next(leg for leg in trade.legs if leg.kind == "cash_substitution")
            if sub.final is None:
                value = self._manager_value(trade, context.now)
                sub.estimate = (trade.plan.substitution_deposit - value if trade.decision.side is Side.CREATE
                                else value + trade.plan.domestic_fixed)
                for order in self._orders(trade, "MANAGER"):
                    if order.remaining:
                        bar = self.prices.get(order.symbol, context.now)
                        if bar is None or bar.time.time() < time(15):
                            stale_substitution.append(order.symbol)
        for (_, symbol), quantity in self.account.positions.items():
            if quantity:
                bar = self.prices.get(symbol, context.now)
                if bar:
                    self.marks[symbol] = bar.close
                if bar is None or bar.time.time() < time(15):
                    stale.append(symbol)
        receivable = sum((leg.outstanding for t in self.trades for leg in t.legs), ZERO)
        equity = self.account.equity(self.marks, receivable)
        self.equity_curve.append(EquityPoint(context.now.date(), equity))
        self.daily.append(dict(date=context.now.date(), cash=self.account.cash, receivables=receivable,
                               holdings_value=equity-self.account.cash-receivable, equity=equity,
                               stale_marks=";".join(sorted(set(stale))),
                               stale_cash_substitution=";".join(sorted(set(stale_substitution)))))
