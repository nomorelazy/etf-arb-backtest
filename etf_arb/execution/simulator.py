"""Configurable-fee, zero-slippage minute-close execution and settlement.

This is not an exchange matching engine. Prices/volume/status and manager
settlement assumptions are exported alongside every run.
"""

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from etf_arb.data.snapshot import component_symbol
from etf_arb.costs import AssetType, TradeFeeModel
from etf_arb.decision.basket import check_creation_basket
from etf_arb.domain import Side
from etf_arb.execution.plan import application_allowed, build_plan
from etf_arb.portfolio.account import CashAccount, ZERO
from etf_arb.portfolio.ledger import LedgerEventType as Event
from etf_arb.report.models import EquityPoint
from etf_arb.rules import TradeFrequencyRule, TradingTimeRule
from etf_arb.rules.tradability import security_trade_block
from etf_arb.rules.cash_substitution import CashSubstitutionTiming
from etf_arb.settlement.cash import CashLeg, trading_day_after, load_payment_dates


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
    fee: Decimal = ZERO
    cancelled: bool = False
    last_block: str = ""
    paused: bool = False
    manager_attempted_at: datetime | None = None

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
        self.fee_model = TradeFeeModel(settings.fees)
        self.account = None
        self.trades, self.orders, self.fills, self.rejections = [], [], [], []
        self.equity_curve, self.daily, self.marks = [], [], {}
        self.started_by_day = {}
        self.frequency = TradeFrequencyRule(settings.min_trade_interval_minutes, settings.max_trades_per_day)
        self.trading_times = TradingTimeRule(settings.blocked_trade_minutes, settings.trading_sessions)
        self.cash_timing = CashSubstitutionTiming(settings.cash_substitution_pricing_days,
                                                settings.cash_substitution_attempt_time)
        self.payment_dates = {}
        self.substitution_pricing = []
        self.substitution_attempts = []
        self.creation_substitutions = []
        self.fee_rows = []
        self.peak_open_cycles = 0

    def _fee_for(self, symbol, side, quantity, price):
        return self.fee_model.calculate(
            asset_type=AssetType.ETF if symbol == "159338.SZ" else AssetType.STOCK,
            side=side, turnover=quantity * price)

    def available(self, at):
        # A pending signal has not started creation/redemption yet. Wait for
        # that actual application timestamp before starting its hourly clock.
        if any(t.applied_at is None for t in self.trades):
            return False
        latest = max((t.applied_at for t in self.trades), default=None)
        return self.frequency.allows(at, last_started_at=latest,
                                     started_today=self.started_by_day.get(at.date(), 0))

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
            self.payment_dates = load_payment_dates(context.resources.config.cash_payment_dates_path)
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
        if self.trading_times.is_blocked(now.time()):
            return "blocked_trade_minute"
        if not self.trading_times.allows(now.time()):
            return "outside_trading_sessions"
        return security_trade_block(context.resources.rules, order.symbol, now, order.side,
                                    self.settings.security_rule_policy)

    def handle(self, decision, context):
        pcf = context.pcfs[decision.etf_code]
        reason = ""
        if not self.available(context.now):
            reason = "application_interval_or_pending_start"
        elif self.trading_times.is_blocked(context.now.time()):
            reason = "blocked_trade_minute"
        elif not self.trading_times.allows(context.now.time()):
            reason = "outside_trading_sessions"
        elif not application_allowed(pcf, context.resources.rules.get(pcf.etf_code, context.now.date()),
                                     decision.side, decision.quantity_units, context.now.time()):
            reason = "application_disabled_or_limit"
        if reason:
            self.rejections.append(dict(ts=context.now, reason=reason))
            return
        basket = None
        if decision.side is Side.CREATE:
            basket = check_creation_basket(pcf, self.prices, context.resources.rules, context.now,
                                           self.settings.security_rule_policy)
            if basket.blocked or basket.data_errors:
                kind = ("unbuyable_and_no_cash_substitution" if basket.blocked
                        else "missing_cash_substitution_data")
                self.rejections.append(dict(ts=context.now,
                    reason=f"{kind}: {basket.blocked | basket.data_errors}"))
                return
        try:
            plan = build_plan(pcf, decision.side, decision.quantity_units,
                              creation_cash_substitutions=basket.cash_substitutions if basket else None)
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
            needed += quantity * bar.close + self._fee_for(symbol, "BUY", quantity, bar.close).total
        if needed > self.account.cash:
            reason = "insufficient_cash"
        if reason:
            self.rejections.append(dict(ts=context.now, reason=reason))
            return
        trade = SimTrade(f"T{len(self.trades)+1:06}", decision, pcf, plan)
        self.trades.append(trade)
        self.peak_open_cycles = max(self.peak_open_cycles,
                                    sum(t.state != "CLOSED" for t in self.trades))
        for component in pcf.components:
            symbol = component_symbol(component) if not component.is_cash_placeholder else None
            if symbol in plan.optional_creation_substitutions:
                self.creation_substitutions.append(dict(ts=context.now, trade_id=trade.trade_id,
                    symbol=symbol, quantity=component.quantity * decision.quantity_units,
                    price=self.prices.get(symbol, context.now, exact=True).close,
                    premium_rate=component.creation_premium_rate,
                    deposit=plan.optional_creation_substitutions[symbol],
                    reason=basket.substitution_reasons[symbol], basis="signal_minute_close_plus_pcf_premium"))
        context.resources.portfolio.open_trade(trade_id=trade.trade_id, opened_at=context.now,
                                               side=decision.side, etf_shares=plan.etf_shares)
        for symbol, quantity in entries.items():
            self._order(trade, symbol, "BUY", quantity, context.now, "ENTRY")
        # An all-cash creation still waits for a later bar before applying.

    def _fill(self, order, trade, context):
        if not order.remaining or order.paused:
            return
        if order.purpose == "MANAGER":
            sub = next(leg for leg in trade.legs if leg.kind == "cash_substitution")
            if order.manager_attempted_at is not None:
                return
            if context.now != sub.attempt_scheduled_at:
                order.last_block = "waiting_for_scheduled_substitution_attempt"
                return
            # Both the scheduled callback and a regular clock can share this
            # timestamp. Even a failed/partial attempt must not run twice.
            order.manager_attempted_at = context.now
        block = self._can_trade(order, context)
        bar = self.prices.get(order.symbol, context.now, exact=True)
        if block or bar is None:
            order.last_block = block or "missing_exact_bar"
            return
        quantity = order.remaining
        cap = self.settings.max_shares_per_minute
        if cap is not None and (order.purpose == "MANAGER" or not self.settings.assume_full_basket_fill):
            quantity = min(quantity, cap)
        if order.purpose != "MANAGER":
            if order.side == "BUY":
                floor = max(trade.plan.estimated_cash, ZERO)
                if trade.decision.side is Side.CREATE:
                    floor += trade.plan.substitution_deposit + trade.plan.domestic_fixed
                available = max(ZERO, self.account.cash - floor)
                if self.settings.assume_full_basket_fill:
                    required = quantity * bar.close + self._fee_for(order.symbol, "BUY", quantity, bar.close).total
                    if available < required:
                        raise ValueError(f"Full-basket assumption cannot be funded: {trade.trade_id}/{order.symbol}")
                else:
                    low, high = 0, quantity // 100
                    while low < high:
                        mid = (low + high + 1) // 2
                        needed = mid * 100 * bar.close + self._fee_for(order.symbol, "BUY", mid * 100, bar.close).total
                        if needed <= available:
                            low = mid
                        else:
                            high = mid - 1
                    quantity = low * 100
            elif quantity < order.remaining:
                quantity = quantity // 100 * 100
        if quantity <= 0:
            order.last_block = "insufficient_cash_or_lot_size"
            return
        fees = self._fee_for(order.symbol, order.side, quantity, bar.close)
        if order.purpose != "MANAGER":
            self.account.fill(context.now, trade.trade_id, order.symbol, order.side, quantity, bar.close, fees=fees)
        order.filled += quantity
        order.value += quantity * bar.close
        order.fee += fees.total
        order.last_block = ""
        self.marks[order.symbol] = bar.close
        self.fills.append(dict(ts=context.now, order_id=order.order_id, trade_id=trade.trade_id,
                               symbol=order.symbol, side=order.side, quantity=quantity, price=bar.close,
                               purpose=order.purpose, fee=fees.total, **asdict(fees), slippage=ZERO,
                               basis="manager_scheduled_close_proxy" if order.purpose == "MANAGER" else "next_minute_close"))
        self.fee_rows.append(dict(ts=context.now, trade_id=trade.trade_id, order_id=order.order_id,
            symbol=order.symbol, side=order.side, purpose=order.purpose,
            turnover=quantity * bar.close, **asdict(fees), total=fees.total,
            charged_to="cash_substitution" if order.purpose == "MANAGER" else "investor_cash"))

    def _fill_full_basket(self, trade, context):
        """Preflight every entry and all application cash before any leg fills.

        This research mode assumes full fills, not unlimited cash or invented
        quotes. Contradictory inputs fail the run without a partial basket.
        Manager T+2 attempts retain their separately configured policy.
        """
        if context.now <= trade.decision.ts:
            return
        if context.now.date() != trade.pcf.effective_date or not application_allowed(
            trade.pcf, context.resources.rules.get(trade.pcf.etf_code, context.now.date()),
            trade.decision.side, trade.decision.quantity_units, context.now.time()
        ):
            raise ValueError(f"Full-basket assumption has no valid application: {trade.trade_id}")
        required = max(trade.plan.estimated_cash, ZERO)
        if trade.decision.side is Side.CREATE:
            required += trade.plan.substitution_deposit + trade.plan.domestic_fixed
        entries = self._orders(trade, "ENTRY")
        for order in entries:
            block = self._can_trade(order, context)
            bar = self.prices.get(order.symbol, context.now, exact=True)
            if block or bar is None:
                raise ValueError(f"Full-basket assumption lacks an executable entry: "
                                 f"{trade.trade_id}/{order.symbol}: {block or 'missing_exact_bar'}")
            required += (order.remaining * bar.close
                         + self._fee_for(order.symbol, "BUY", order.remaining, bar.close).total)
        if required > self.account.cash:
            raise ValueError(f"Full-basket assumption cannot be funded: {trade.trade_id}; "
                             f"required={required}, available={self.account.cash}")
        for order in entries:
            self._fill(order, trade, context)
        self._apply(trade, context)

    def _apply(self, trade, context):
        plan, side, now = trade.plan, trade.decision.side, context.now
        if not self.trading_times.allows(now.time()):
            return
        if now <= trade.decision.ts or any(o.remaining for o in self._orders(trade, "ENTRY")):
            return
        rule = context.resources.rules.get(trade.pcf.etf_code, now.date())
        if now.date() != trade.pcf.effective_date or not application_allowed(
            trade.pcf, rule, side, trade.decision.quantity_units, now.time()
        ):
            self._hold(trade, context, "application_cutoff_or_disabled")
            return
        deposit = max(plan.estimated_cash, ZERO)
        due_cash = deposit + (plan.substitution_deposit + plan.domestic_fixed if side is Side.CREATE else ZERO)
        if self.account.cash < due_cash:
            self._hold(trade, context, "insufficient_application_cash")
            return
        trade.applied_at, trade.cash_deposit, trade.state = now, deposit, "APPLIED"
        self.account.post(now, trade.trade_id, Event.CREATE_SUBMITTED if side is Side.CREATE else Event.REDEEM_SUBMITTED,
                          cash=-due_cash)
        self.started_by_day[now.date()] = self.started_by_day.get(now.date(), 0) + 1
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
        sub = trade.legs[1]
        sub.pricing_deadline, sub.notice_due_on, sub.latest_payment_on = self.cash_timing.dates(
            now.date(), context.resources.config.trading_days,
            context.resources.config.settlement_business_days)
        sub.attempt_scheduled_at = self.cash_timing.attempt_at(sub.pricing_deadline)
        if sub.attempt_scheduled_at is not None and sub.attempt_scheduled_at >= now:
            context.schedule(sub.attempt_scheduled_at,
                lambda ctx, t=trade: self._attempt_substitution(t, ctx),
                f"cash.substitution_attempt.{trade.trade_id}")
        if sub.pricing_deadline is not None:
            context.schedule(datetime.combine(sub.pricing_deadline, time(15)),
                lambda ctx, t=trade: self._price_substitution_deadline(t, ctx),
                f"cash.pricing_deadline.{trade.trade_id}")
        for leg in trade.legs:
            leg.payment_at = self.payment_dates.get((trade.trade_id, leg.kind))
            if leg.payment_at is not None:
                if leg.payment_at <= now:
                    raise ValueError("Cash payment timestamp must follow application confirmation")
                if leg.kind == "cash_substitution" and leg.notice_due_on is not None and leg.payment_at.date() < leg.notice_due_on:
                    raise ValueError("Substitution payment precedes the ordinary notice date; verify the supplied record")
                leg.payment_basis = "supplied_payment_timestamp"
                leg.due = leg.payment_at.date()
                if leg.payment_at.date() in context.resources.config.trading_days:
                    context.schedule(leg.payment_at,
                        lambda ctx, t=trade, l=leg: self._record_payment(t, l, ctx),
                        f"cash.payment.{trade.trade_id}.{leg.kind}")
        for symbol, quantity in plan.manager.items():
            self._order(trade, symbol, "BUY" if side is Side.CREATE else "SELL", quantity, now, "MANAGER")
        # Cash-substitution proxy orders wait for the scheduled T+2 attempt.

    def _attempt_substitution(self, trade, context):
        for order in self._orders(trade, "MANAGER"):
            if not order.remaining or order.paused or order.manager_attempted_at is not None:
                continue
            requested, before = order.remaining, order.filled
            self._fill(order, trade, context)
            filled = order.filled - before
            self.substitution_attempts.append(dict(trade_id=trade.trade_id, order_id=order.order_id,
                symbol=order.symbol, ts=context.now, side=order.side, requested_quantity=requested,
                filled_quantity=filled, unfilled_quantity=order.remaining,
                result="FILLED" if filled == requested else "PARTIAL" if filled else "NO_FILL",
                reason=order.last_block or ("quantity_cap" if filled < requested else "")))
        self._settle(trade, context)

    def _manager_value(self, trade, now):
        result = trade.plan.manager_fixed
        orders = self._orders(trade, "MANAGER")
        filled_by_symbol = {o.symbol: o for o in orders}
        for symbol, required in trade.plan.manager.items():
            order = filled_by_symbol.get(symbol)
            filled = order.filled if order else 0
            if order:
                result += order.value
                if order.fee:
                    result += order.fee if trade.decision.side is Side.CREATE else -order.fee
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
        if sub.final is None:
            manager_value = self._manager_value(trade, now)
            sub.estimate = (plan.substitution_deposit - manager_value if trade.decision.side is Side.CREATE
                            else manager_value + plan.domestic_fixed)
        for leg in trade.legs:
            if leg.payment_at is not None:
                continue  # Exact payment callbacks are independent of trading windows.
            if leg.paid or leg.final is None or leg.due is None or now.date() < leg.due:
                continue
            self._record_payment(trade, leg, context)

    def _record_payment(self, trade, leg, context):
        if leg.paid:
            return
        if leg.final is None:
            trade.reason = "payment_amount_not_determined"
            return
        if leg.priced_at is not None and context.now < leg.priced_at:
            raise ValueError("Payment cannot precede pricing")
        if self.account.cash + leg.final < 0:
            trade.reason = "insufficient_cash_for_settlement"
            return
        self.account.post(context.now, trade.trade_id,
                          Event.CASH_SUBSTITUTION_SETTLED if leg.kind == "cash_substitution" else Event.CASH_SETTLED,
                          cash=leg.final)
        leg.paid, leg.paid_at = True, context.now
        self._close_if_done(trade, context)

    def _hold(self, trade, context, reason):
        if self.settings.assume_full_basket_fill:
            raise ValueError(f"Full-basket assumption was not met: {trade.trade_id}: {reason}")
        trade.state, trade.reason = "HELD_PENDING_POLICY", reason
        for order in self._orders(trade, "ENTRY"):
            if order.remaining:
                order.paused = True
                order.last_block = "awaiting_disposition_policy"
        self.account.post(context.now, trade.trade_id, Event.TRADE_HELD)

    def _price_substitution_deadline(self, trade, context):
        sub = next(leg for leg in trade.legs if leg.kind == "cash_substitution")
        if sub.final is not None or sub.pricing_deadline != context.now.date():
            return
        outstanding = [order for order in self._orders(trade, "MANAGER") if order.remaining]
        prices = {}
        for order in outstanding:
            at_close = datetime.combine(context.now.date(), time(15))
            bar = self.prices.get(order.symbol, at_close, exact=True)
            if bar is None:
                trade.reason = "missing_substitution_deadline_close"
                return  # Never replace a missing T+2 close with a future price.
            prices[order.order_id] = bar.close
        fee_sign = 1 if trade.decision.side is Side.CREATE else -1
        value = trade.plan.manager_fixed + sum(
            (o.value for o in self._orders(trade, "MANAGER")), ZERO)
        for order in self._orders(trade, "MANAGER"):
            if order.fee:
                value += fee_sign * order.fee
        for order in outstanding:
            quantity, price = order.remaining, prices[order.order_id]
            value += quantity * price
            self.substitution_pricing.append(dict(trade_id=trade.trade_id, symbol=order.symbol,
                quantity=quantity, price=price, ts=datetime.combine(context.now.date(), time(15)),
                basis="unfilled_quantity_at_deadline_close"))
            order.cancelled = True  # Stop only the proxy order; this is not an investor fill.
            order.last_block = "valued_at_pricing_deadline_close"
        sub.final = (trade.plan.substitution_deposit - value if trade.decision.side is Side.CREATE
                     else value + trade.plan.domestic_fixed)
        sub.estimate = sub.final
        sub.known_on, sub.priced_at = context.now.date(), context.now
        sub.pricing_basis = ("filled_proxy_values_plus_deadline_close" if outstanding
                             else "completed_manager_close_proxy")
        if sub.payment_at is None and self.settings.cash_substitution_lag is not None:
            sub.due = trading_day_after(context.resources.config.trading_days, trade.applied_at.date(),
                                       self.settings.cash_substitution_lag)
            sub.payment_basis = "explicit_research_lag"
        self._settle(trade, context)

    def _close_if_done(self, trade, context):
        if trade.state in {"BUILD", "HELD_PENDING_POLICY", "CLOSED"} or self.account.holdings(trade.trade_id):
            return
        if any(o.remaining for o in self._orders(trade)) or any(not leg.paid for leg in trade.legs):
            return
        context.resources.portfolio.settle_trade(trade_id=trade.trade_id, settled_at=context.now,
            pnl=self.account.trade_cash.get(trade.trade_id, ZERO),
            amount=sum((o.value for o in self._orders(trade) if o.symbol == "159338.SZ" and o.purpose != "MANAGER"), ZERO))
        trade.state = "CLOSED"

    def on_time(self, context):
        if not self.trading_times.allows(context.now.time()):
            return
        for trade in self.trades:
            if trade.state in {"CLOSED", "HELD_PENDING_POLICY"}:
                continue
            if trade.state == "BUILD" and self.settings.assume_full_basket_fill:
                self._fill_full_basket(trade, context)
                continue  # Received assets still sell only on a later minute.
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
                self._hold(trade, context, "basket_incomplete_at_close")
            elif trade.state == "APPLIED":
                self._price_substitution_deadline(trade, context)
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
