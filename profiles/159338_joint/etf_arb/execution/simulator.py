"""Configurable-fee, zero-slippage minute-close execution and settlement.

This is not an exchange matching engine. Prices/volume/status and manager
settlement assumptions are exported alongside every run.
"""

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json

from etf_arb.data.snapshot import FULL_FLOW_ETFS, component_symbol
from etf_arb.costs import AssetType, TradeFeeModel
from etf_arb.decision.basket import check_creation_basket
from etf_arb.domain import Side
from etf_arb.execution.plan import application_allowed, build_plan
from etf_arb.portfolio.account import CashAccount, ZERO
from etf_arb.portfolio.ledger import LedgerEventType as Event
from etf_arb.report.models import EquityPoint
from etf_arb.report.attribution import signal_reference
from etf_arb.rules import TradeFrequencyRule, TradingTimeRule
from etf_arb.rules.tradability import security_trade_block
from etf_arb.rules.price_limit_filter import price_limit_reason
from etf_arb.rules.cash_substitution import CashSubstitutionTiming
from etf_arb.rules.security_calendar import exception_close, notice_dates, substitution_deadline
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
    next_minute_attempted_at: datetime | None = None
    last_cross_market_attempted_at: datetime | None = None
    waiting_after_limit_down: bool = False
    waiting_after_suspension: bool = False
    pricing_deadline: date | None = None
    deadline_basis: str = ""
    attempt_scheduled_at: datetime | None = None
    pricing_close_at: datetime | None = None
    deadline_valuation_price: Decimal | None = None

    @property
    def waiting_for_stock_sale(self):
        return self.waiting_after_limit_down or self.waiting_after_suspension

    @property
    def last_sh_attempted_at(self):
        return self.last_cross_market_attempted_at if self.symbol.endswith(".SH") else None

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
    redemption_prices_locked_at: datetime | None = None
    delayed_redemption: dict[str, str] = field(default_factory=dict)
    local_retry_scheduled_at: datetime | None = None
    cross_retry_scheduled_at: datetime | None = None
    signal_reference: dict = field(default_factory=dict)


class ArbitrageSimulator:
    def __init__(self, prices, settings, *, etf_code="159338", price_limit_filter=None, creation_guard=None, execution_limits=None):
        self.prices, self.settings = prices, settings
        self.creation_guard = creation_guard
        self.price_limit_filter = price_limit_filter
        self.execution_limits = (execution_limits if execution_limits is not None else
                                 creation_guard.limits if creation_guard is not None else price_limit_filter)
        self.execution_limit_blocks = []
        self._last_blocked_minute = {}
        self.price_limit_application_blocks = []
        self.etf_symbol = FULL_FLOW_ETFS[etf_code]
        self.proxy_market = "SZ" if self.etf_symbol.endswith(".SH") else "SH"
        self.fee_model = TradeFeeModel(settings.fees)
        self.account = None
        self.trades, self.orders, self.fills, self.rejections = [], [], [], []
        self._orders_by_trade: dict[str, list[SimOrder]] = {}
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
            asset_type=AssetType.ETF if symbol == self.etf_symbol else AssetType.STOCK,
            side=side, turnover=quantity * price)

    def available(self, at):
        # A pending signal has not started creation/redemption yet. Wait for
        # that actual application timestamp before starting its hourly clock.
        if any(t.applied_at is None and t.state not in {"UNWINDING", "UNWOUND"} for t in self.trades):
            return False
        latest = max((t.applied_at for t in self.trades if t.applied_at is not None), default=None)
        return self.frequency.allows(at, last_started_at=latest,
                                     started_today=self.started_by_day.get(at.date(), 0))

    def _order(self, trade, symbol, side, quantity, now, purpose, not_before=None):
        if quantity:
            order = SimOrder(f"O{len(self.orders)+1:07}", trade.trade_id, symbol, side,
                             quantity, now, purpose, not_before or now.date())
            self.orders.append(order)
            self._orders_by_trade.setdefault(trade.trade_id, []).append(order)

    def _orders(self, trade, purpose=None):
        orders = self._orders_by_trade.get(trade.trade_id, ())
        return list(orders) if purpose is None else [order for order in orders if order.purpose == purpose]

    def on_day_open(self, context, *, signal_symbols=()):
        if self.account is None:
            self.account = CashAccount(context.resources.config.starting_cash, context.resources.portfolio)
            self.payment_dates = load_payment_dates(context.resources.config.cash_payment_dates_path)
            from datetime import timedelta
            self.equity_curve.append(EquityPoint(context.now.date() - timedelta(days=1), self.account.cash))
        symbols = {self.etf_symbol}
        symbols.update(signal_symbols)
        for pcf in context.pcfs.values():
            symbols.update(component_symbol(c) for c in pcf.components if not c.is_cash_placeholder)
        for order in self.orders:
            if order.remaining:
                symbols.add(order.symbol)
        symbols.update(symbol for (_, symbol), q in self.account.positions.items() if q)
        self.prices.load(context.now.date(), symbols)
        if self.execution_limits is not None:
            # Pending constituents can leave the PCF before their sale. Use today's
            # limit reference for them too, including days reserved for settlement.
            pending = []
            for trade in self.trades:
                if trade.state not in {"APPLIED", "UNWINDING"}:
                    continue
                symbols_pending = {order.symbol for order in self._orders(trade)
                                   if order.remaining and order.symbol != self.etf_symbol}
                if symbols_pending:
                    pending.extend(c for c in trade.pcf.components if not c.is_cash_placeholder
                                   and component_symbol(c) in symbols_pending)
            pcf, = context.pcfs.values()
            check_basket = context.now.date() <= context.resources.config.end_date
            options = dict(check_basket=check_basket, held_components=pending)
            initialized = []
            if self.creation_guard is not None:
                self.creation_guard.on_day_open(pcf, context.resources.rules, **options)
                initialized.append(self.creation_guard.limits)
            for limits in (self.execution_limits, self.price_limit_filter):
                if limits is not None and limits not in initialized:
                    limits.on_day_open(pcf, context.resources.rules, **options)
                    initialized.append(limits)
        for trade in self.trades:
            if trade.applied_at:
                for leg in trade.legs:
                    leg.observe_cash_component(
                        context.pcfs[trade.pcf.etf_code], trade.applied_at.date(), trade.decision.side,
                        trade.decision.quantity_units, trade.cash_deposit,
                        context.resources.config.trading_days, self.settings.cash_difference_lag,
                        require_next_trading_day=self.settings.settle_estimated_cash_at_application,
                    )

    def _can_trade(self, order, context, *, application_minute=False, exchange_window=False):
        now = context.now
        if (now < order.submitted or (now == order.submitted and not application_minute)
                or now.date() < order.not_before_day):
            return "waiting_for_later_bar_or_sell_availability"
        if exchange_window:
            if not self.cash_timing.allows_sh_attempt(now):
                return "outside_exchange_minutes"
        elif self.trading_times.is_blocked(now.time()):
            return "blocked_trade_minute"
        elif not self.trading_times.allows(now.time()):
            return "outside_trading_sessions"
        block = security_trade_block(context.resources.rules, order.symbol, now, order.side,
                                     self.settings.security_rule_policy)
        if (not block and self.creation_guard is not None and order.purpose in {"ENTRY", "MANAGER"}
                and order.side == "BUY" and order.symbol != self.etf_symbol):
            block = self.creation_guard.buy_block(order.symbol, now)
        return block

    def handle(self, decision, context):
        if not self.settings.allows_side(decision.side):
            self.rejections.append(dict(ts=context.now, reason="direction_disabled"))
            return
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
        if self.price_limit_filter is not None:
            blocked = self.price_limit_filter.blocked(context.now)
            if blocked:
                self.rejections.append(dict(ts=context.now,
                    reason=f"{price_limit_reason(blocked)}: {json.dumps(blocked, ensure_ascii=False)}"))
                return
        basket = None
        if decision.side is Side.CREATE:
            basket = check_creation_basket(pcf, self.prices, context.resources.rules, context.now,
                                           self.settings.security_rule_policy, guard=self.creation_guard,
                                           reference_limits=self.execution_limits)
            if basket.blocked or basket.data_errors:
                kind = basket.failure_reason
                self.rejections.append(dict(ts=context.now,
                    reason=f"{kind}: {basket.blocked | basket.data_errors}"))
                return
        try:
            plan = build_plan(pcf, decision.side, decision.quantity_units,
                              creation_cash_substitutions=basket.cash_substitutions if basket else None)
        except ValueError as error:
            self.rejections.append(dict(ts=context.now, reason=str(error)))
            return
        if self.settings.market_split_execution and not self.trading_times.has_next_minute(
            context.now, context.resources.config.clock_times
        ):
            self.rejections.append(dict(ts=context.now, reason="no_exact_next_minute_for_market_split"))
            return
        entries = plan.physical if decision.side is Side.CREATE else {self.etf_symbol: plan.etf_shares}
        needed = max(self._application_cash_due(plan, decision.side), ZERO)
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
        def observed_price(symbol):
            bar = self.prices.get(symbol, context.now)
            return None if bar is None else bar.close
        trade.signal_reference = signal_reference(plan, pcf, decision.quantity_units, observed_price)
        self.trades.append(trade)
        self.peak_open_cycles = max(self.peak_open_cycles,
                                    sum(t.state != "CLOSED" for t in self.trades))
        for component in pcf.components:
            symbol = component_symbol(component) if not component.is_cash_placeholder else None
            if symbol in plan.optional_creation_substitutions:
                price, reference = basket.deposit_prices[symbol]
                observed = self.prices.get(symbol, context.now, exact=True)
                self.creation_substitutions.append(dict(ts=context.now, trade_id=trade.trade_id,
                    symbol=symbol, quantity=component.quantity * decision.quantity_units,
                    price=price, observed_price=observed.close if observed is not None else None,
                    observation_stage="signal",
                    premium_rate=component.creation_premium_rate,
                    deposit=plan.optional_creation_substitutions[symbol],
                    reason=basket.substitution_reasons[symbol], basis=reference + "_plus_pcf_premium"))
        context.resources.portfolio.open_trade(trade_id=trade.trade_id, opened_at=context.now,
                                               side=decision.side, etf_shares=plan.etf_shares)
        for symbol, quantity in entries.items():
            self._order(trade, symbol, "BUY", quantity, context.now, "ENTRY")
        if self.settings.entry_at_signal_time:
            self._fill_full_basket(trade, context)
        # Application remains on a later bar, including all-cash creation.

    def _fill(self, order, trade, context, *, next_minute=False, proxy_window=False, at_signal=False,
              local_limit_retry=False):
        if not order.remaining or order.paused:
            return
        # The independent retry callback owns the remaining physical shares.
        if order.purpose == "EXIT" and order.waiting_for_stock_sale and not local_limit_retry:
            return
        if at_signal and (not self.settings.entry_at_signal_time or order.purpose != "ENTRY"
                          or order.side != "BUY" or context.now != trade.decision.ts):
            raise ValueError("Signal-time fills require a configured entry BUY at the signal timestamp")
        if next_minute:
            valid_redemption_exit = (trade.decision.side is Side.REDEEM and order.purpose == "EXIT"
                                     and order.symbol in trade.plan.physical)
            valid_creation_exit = (self.settings.entry_at_signal_time and trade.decision.side is Side.CREATE
                                   and order.purpose == "EXIT" and order.symbol == self.etf_symbol
                                   and order.side == "SELL")
            if (not self.settings.market_split_execution or not (valid_redemption_exit or valid_creation_exit)
                    or trade.applied_at != context.now
                    or context.now != trade.decision.ts + timedelta(minutes=1)):
                raise ValueError("Next-minute fills require a confirmed market-split t+1 application")
        if order.purpose == "MANAGER":
            if self._uses_cross_market_window(order):
                if not proxy_window:
                    return
                if (trade.applied_at is None or context.now < trade.applied_at
                        or (order.pricing_deadline is not None and
                            context.now > datetime.combine(order.pricing_deadline, time(15)))):
                    raise ValueError("Cross-market proxy attempt is outside application-to-deadline window")
                if order.last_cross_market_attempted_at == context.now:
                    return
                order.last_cross_market_attempted_at = context.now
                if context.now == trade.applied_at:
                    order.next_minute_attempted_at = context.now
            else:
                if order.manager_attempted_at is not None:
                    return
                if context.now != order.attempt_scheduled_at:
                    order.last_block = "waiting_for_scheduled_substitution_attempt"
                    return
                # Scheduled callback and regular clock may share a timestamp.
                order.manager_attempted_at = context.now
        block = self._can_trade(order, context, application_minute=next_minute or proxy_window or at_signal,
                                exchange_window=proxy_window or local_limit_retry)
        bar = self.prices.get(order.symbol, context.now, exact=True)
        if block == "stock_suspended":
            if order.purpose == "MANAGER":
                order.waiting_after_suspension = True
            elif trade.decision.side is Side.REDEEM and order.purpose == "EXIT" and order.side == "SELL":
                order.waiting_after_suspension = True
                self._schedule_local_redemption_retry(trade, context)
        if (not block and bar is not None and order.waiting_after_suspension and order.side == "BUY"
                and self.creation_guard is None and self.execution_limits is not None):
            status = self.execution_limits.status(order.symbol, context.now)
            if status and not status.startswith("limit_down:"):
                block = status
        if (not block and bar is not None and self.execution_limits is not None
                and trade.decision.side is Side.REDEEM and order.side == "SELL"
                and order.purpose in {"EXIT", "MANAGER"}):
            status = self.execution_limits.status(order.symbol, context.now)
            if status and status.startswith("limit_down:"):
                if order.purpose == "EXIT" and not order.waiting_after_limit_down:
                    self._schedule_local_redemption_retry(trade, context)
                order.waiting_after_limit_down = True
                block = status
            elif order.waiting_for_stock_sale and status and status.startswith("unknown:"):
                block = status
            elif order.purpose == "MANAGER":
                order.waiting_after_limit_down = False
        if block or bar is None:
            order.last_block = block or "missing_exact_bar"
            if block.startswith(("limit_down:", "limit_up:")) and self._last_blocked_minute.get(order.order_id) != context.now:
                self._last_blocked_minute[order.order_id] = context.now
                self.execution_limit_blocks.append(dict(
                    ts=context.now, trade_id=trade.trade_id, order_id=order.order_id,
                    symbol=order.symbol, side=order.side, purpose=order.purpose,
                    unfilled_quantity=order.remaining, close=bar.close, reason=block))
            return
        if order.purpose == "UNWIND" and order.symbol != self.etf_symbol:
            status = self.price_limit_filter.status(order.symbol, context.now)
            if status is not None and not status.startswith("limit_up:"):
                order.last_block = status
                return
        quantity = order.remaining
        cap = self.settings.max_shares_per_minute
        uncapped_window = next_minute or (proxy_window and self.settings.market_split_execution)
        if cap is not None and not uncapped_window and (order.purpose == "MANAGER" or not self.settings.assume_full_basket_fill):
            quantity = min(quantity, cap)
        if order.purpose != "MANAGER":
            if order.side == "BUY":
                floor = max(self._application_cash_due(trade.plan, trade.decision.side), ZERO)
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
        resumed = order.waiting_after_suspension
        order.filled += quantity
        if not order.remaining:
            order.waiting_after_limit_down = False
            order.waiting_after_suspension = False
        order.value += quantity * bar.close
        order.fee += fees.total
        order.last_block = ""
        self.marks[order.symbol] = bar.close
        self.fills.append(dict(ts=context.now, order_id=order.order_id, trade_id=trade.trade_id,
                               symbol=order.symbol, side=order.side, quantity=quantity, price=bar.close,
                               purpose=order.purpose, fee=fees.total, **asdict(fees), slippage=ZERO,
                               basis=(("manager_next_minute_close_proxy" if context.now == trade.applied_at
                                       else "manager_first_tradable_close_proxy") if proxy_window
                                      else "manager_scheduled_close_proxy")
                                     if order.purpose == "MANAGER" else
                                     "first_tradable_close_after_suspension" if local_limit_retry and resumed else
                                     "first_tradable_close_after_limit_down" if local_limit_retry else
                                     "signal_minute_close" if at_signal else "next_minute_close"))
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
        at_signal = self.settings.entry_at_signal_time and context.now == trade.decision.ts
        if context.now <= trade.decision.ts and not at_signal:
            return
        if self.settings.market_split_execution and not at_signal and context.now != trade.decision.ts + timedelta(minutes=1):
            raise ValueError(f"Market-split next-minute application window missed: {trade.trade_id}")
        application_at = context.now
        if at_signal:
            next_clock = next((clock for clock in context.resources.config.clock_times
                               if clock > context.now.time() and self.trading_times.allows(clock)), None)
            if next_clock is None:
                raise ValueError(f"No later application minute after signal-time entry: {trade.trade_id}")
            application_at = datetime.combine(context.now.date(), next_clock)
        if application_at.date() != trade.pcf.effective_date or not application_allowed(
            trade.pcf, context.resources.rules.get(trade.pcf.etf_code, application_at.date()),
            trade.decision.side, trade.decision.quantity_units, application_at.time()
        ):
            raise ValueError(f"Full-basket assumption has no valid application: {trade.trade_id}")
        required = max(self._application_cash_due(trade.plan, trade.decision.side), ZERO)
        entries = [order for order in self._orders(trade, "ENTRY") if order.remaining]
        for order in entries:
            block = self._can_trade(order, context, application_minute=at_signal)
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
            self._fill(order, trade, context, at_signal=at_signal)
        self._apply(trade, context)

    def _estimated_cash_due(self, plan, side):
        if self.settings.settle_estimated_cash_at_application:
            return plan.estimated_cash if side is Side.CREATE else -plan.estimated_cash
        return max(plan.estimated_cash, ZERO)

    def _application_cash_due(self, plan, side):
        estimated = self._estimated_cash_due(plan, side)
        return estimated + plan.substitution_deposit + plan.domestic_fixed if side is Side.CREATE else estimated

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
        if self.price_limit_filter is not None:
            blocked = self.price_limit_filter.blocked(now)
            if blocked:
                self._block_price_limit_application(trade, context, blocked)
                return
        deposit = self._estimated_cash_due(plan, side)
        due_cash = self._application_cash_due(plan, side)
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
            self.account.post(now, trade.trade_id, Event.SECURITIES_AVAILABLE, symbol=self.etf_symbol, quantity=plan.etf_shares)
            self._order(trade, self.etf_symbol, "SELL", plan.etf_shares, now, "EXIT")
            cash_estimate = deposit - plan.estimated_cash
            sub_estimate = plan.substitution_deposit - self._manager_value(trade, now)
        else:
            self.account.post(now, trade.trade_id, Event.APPLICATION_CONFIRMED, symbol=self.etf_symbol, quantity=-plan.etf_shares)
            for symbol, quantity in plan.physical.items():
                self.account.post(now, trade.trade_id, Event.SECURITIES_AVAILABLE, symbol=symbol, quantity=quantity)
                self._order(trade, symbol, "SELL", quantity, now, "EXIT")
            cash_estimate = deposit + plan.estimated_cash
            sub_estimate = self._manager_value(trade, now) + plan.domestic_fixed
        trade.legs = [CashLeg("cash_difference", cash_estimate), CashLeg("cash_substitution", sub_estimate)]
        for symbol, quantity in plan.manager.items():
            self._order(trade, symbol, "BUY" if side is Side.CREATE else "SELL", quantity, now, "MANAGER")
        self._schedule_substitution(trade, context)
        for leg in trade.legs:
            leg.payment_at = self.payment_dates.get((trade.trade_id, leg.kind))
            if leg.payment_at is not None:
                if leg.payment_at <= now:
                    raise ValueError("Cash payment timestamp must follow application confirmation")
                if (leg.kind == "cash_substitution" and leg.pricing_deadline is not None
                        and leg.payment_at < datetime.combine(leg.pricing_deadline, time(15))):
                    raise ValueError("Supplied substitution payment precedes final pricing")
                if (leg.kind == "cash_substitution" and not self._uses_security_calendar()
                        and leg.notice_due_on is not None and leg.payment_at.date() < leg.notice_due_on):
                    raise ValueError("Substitution payment precedes the ordinary notice date; verify the supplied record")
                leg.payment_basis = "supplied_payment_timestamp"
                leg.due = leg.payment_at.date()
                if leg.payment_at.date() in context.resources.config.trading_days:
                    context.schedule(leg.payment_at,
                        lambda ctx, t=trade, l=leg: self._record_payment(t, l, ctx),
                        f"cash.payment.{trade.trade_id}.{leg.kind}")
        if self.settings.market_split_execution:
            self._attempt_cross_market_substitution(trade, context)
            if side is Side.REDEEM or self.settings.entry_at_signal_time:
                for order in self._orders(trade, "EXIT"):
                    self._fill(order, trade, context, next_minute=True)
            if side is Side.REDEEM:
                for order in self._orders(trade):
                    if order.remaining:
                        trade.delayed_redemption[order.symbol] = order.last_block
                if not trade.delayed_redemption:
                    trade.redemption_prices_locked_at = now

    def _uses_security_calendar(self):
        return self.etf_symbol == "159338.SZ" and self.cash_timing.pricing_trading_days == 2

    def _schedule_substitution(self, trade, context):
        config = context.resources.config
        applied_on = trade.applied_at.date()
        default_deadline, _, _ = self.cash_timing.dates(applied_on, config.trading_days)
        orders = self._orders(trade, "MANAGER")
        attempts = set()
        for order in orders:
            if self._uses_security_calendar():
                deadline, capped = substitution_deadline(
                    order.symbol, applied_on, config.trading_days, context.resources.rules)
                order.deadline_basis = ("exchange_20_day_cap" if capped else "second_normal_session"
                                        if deadline is not None else "undetermined")
            else:
                deadline = default_deadline
                order.deadline_basis = "configured_trading_sessions"
            order.pricing_deadline = deadline
            if (deadline is not None and order.deadline_basis != "exchange_20_day_cap"
                    and not self._uses_cross_market_window(order)):
                order.attempt_scheduled_at = self.cash_timing.attempt_at(deadline)
                if order.attempt_scheduled_at >= trade.applied_at:
                    attempts.add(order.attempt_scheduled_at)

        sub = trade.legs[1]
        deadlines = {order.pricing_deadline for order in orders} or {default_deadline}
        sub.pricing_deadline = None if None in deadlines else max(deadlines)
        cap_at_final_deadline = any(order.deadline_basis == "exchange_20_day_cap"
                                    and order.pricing_deadline == sub.pricing_deadline for order in orders)
        sub.notice_due_on, sub.latest_payment_on = notice_dates(
            sub.pricing_deadline, config.settlement_business_days,
            cap_exchange_days=config.trading_days if cap_at_final_deadline else None)
        sub.attempt_scheduled_at = next(iter(attempts)) if len(attempts) == 1 else None
        for at in sorted(attempts):
            context.schedule(at, lambda ctx: self._attempt_substitution(trade, ctx),
                             f"cash.substitution_attempt.{trade.trade_id}")
        for deadline in sorted(day for day in deadlines if day is not None):
            context.schedule(datetime.combine(deadline, time(15)),
                             lambda ctx: self._price_substitution_deadline(trade, ctx),
                             f"cash.pricing_deadline.{trade.trade_id}")

    def _block_price_limit_application(self, trade, context, blocked):
        now = context.now
        trade.state, trade.reason = "UNWINDING", price_limit_reason(blocked)
        self.price_limit_application_blocks.append(dict(
            ts=now, trade_id=trade.trade_id, side=trade.decision.side.value,
            reason=trade.reason, component_reasons=json.dumps(blocked, ensure_ascii=False),
        ))
        self.account.post(now, trade.trade_id, Event.APPLICATION_REJECTED)
        next_day = trading_day_after(context.resources.config.trading_days, now.date(), 1)
        if next_day is not None:
            for symbol, quantity in self.account.holdings(trade.trade_id).items():
                self._order(trade, symbol, "SELL", quantity, now, "UNWIND", not_before=next_day)
        self._close_if_done(trade, context)

    def _schedule_local_redemption_retry(self, trade, context):
        if trade.local_retry_scheduled_at is not None:
            return
        next_at = self.cash_timing.next_sh_attempt(context.now, context.resources.config.trading_days, None)
        if next_at is not None:
            trade.local_retry_scheduled_at = next_at
            context.schedule(next_at, lambda ctx: self._retry_local_redemption(trade, ctx),
                             f"redemption.retry.{trade.trade_id}")

    def _retry_local_redemption(self, trade, context):
        trade.local_retry_scheduled_at = None
        pending = False
        for order in self._orders(trade, "EXIT"):
            if order.remaining and order.waiting_for_stock_sale:
                self._fill(order, trade, context, local_limit_retry=True)
                pending = pending or bool(order.remaining)
        if pending:
            self._schedule_local_redemption_retry(trade, context)
        else:
            self._close_if_done(trade, context)

    def _cross_market_proxy(self, symbol):
        return symbol.endswith(f".{self.proxy_market}")

    def _uses_cross_market_window(self, order):
        return self._cross_market_proxy(order.symbol) and (
            self.settings.market_split_execution or order.last_cross_market_attempted_at is not None)

    def _attempt_substitution(self, trade, context):
        for order in self._orders(trade, "MANAGER"):
            if not order.remaining or order.paused:
                continue
            if self.settings.market_split_execution and self._cross_market_proxy(order.symbol):
                continue
            if order.manager_attempted_at is not None or order.attempt_scheduled_at != context.now:
                continue
            requested, before = order.remaining, order.filled
            self._fill(order, trade, context)
            filled = order.filled - before
            self.substitution_attempts.append(dict(trade_id=trade.trade_id, order_id=order.order_id,
                symbol=order.symbol, ts=context.now, side=order.side, requested_quantity=requested,
                filled_quantity=filled, unfilled_quantity=order.remaining,
                result="FILLED" if filled == requested else "PARTIAL" if filled else "NO_FILL",
                reason=order.last_block or ("quantity_cap" if filled < requested else ""),
                timing="scheduled_substitution"))
            if (not self.settings.market_split_execution and self._cross_market_proxy(order.symbol)
                    and order.side == "SELL" and order.last_block.startswith("limit_down:")):
                # A legacy scheduled sale now waits for the first unblocked minute.
                order.last_cross_market_attempted_at = context.now
        self._settle(trade, context)
        if not self.settings.market_split_execution:
            self._schedule_cross_market_retry(trade, context)

    def _attempt_cross_market_substitution(self, trade, context, *, schedule_next=True):
        sub = next(leg for leg in trade.legs if leg.kind == "cash_substitution")
        if trade.cross_retry_scheduled_at == context.now:
            trade.cross_retry_scheduled_at = None
        if sub.final is not None or trade.state != "APPLIED":
            return
        for order in self._orders(trade, "MANAGER"):
            if (not self._uses_cross_market_window(order) or not order.remaining or order.paused
                    or order.last_cross_market_attempted_at == context.now
                    or (order.pricing_deadline is not None and context.now.date() > order.pricing_deadline)):
                continue
            requested, before = order.remaining, order.filled
            self._fill(order, trade, context, proxy_window=True)
            filled = order.filled - before
            self.substitution_attempts.append(dict(trade_id=trade.trade_id, order_id=order.order_id,
                symbol=order.symbol, ts=context.now, side=order.side, requested_quantity=requested,
                filled_quantity=filled, unfilled_quantity=order.remaining,
                result="FILLED" if filled == requested else "PARTIAL" if filled else "NO_FILL",
                reason=order.last_block or ("quantity_cap" if filled < requested else ""),
                timing=(f"{self.proxy_market.lower()}_next_minute" if context.now == trade.applied_at
                        else f"{self.proxy_market.lower()}_first_tradable_window")))
        self._settle(trade, context)
        if schedule_next:
            self._schedule_cross_market_retry(trade, context)

    def _schedule_cross_market_retry(self, trade, context):
        if trade.cross_retry_scheduled_at is not None:
            return
        pending = any(self._uses_cross_market_window(o) and o.remaining and not o.paused
                      and (o.pricing_deadline is None or context.now < datetime.combine(o.pricing_deadline, time(15)))
                      for o in self._orders(trade, "MANAGER"))
        if pending:
            sub = next(leg for leg in trade.legs if leg.kind == "cash_substitution")
            next_at = self.cash_timing.next_sh_attempt(
                context.now, context.resources.config.trading_days, sub.pricing_deadline)
            if next_at is not None:
                trade.cross_retry_scheduled_at = next_at
                context.schedule(next_at, lambda ctx, t=trade: self._attempt_cross_market_substitution(t, ctx),
                                 f"cash.{self.proxy_market.lower()}_first_tradable.{trade.trade_id}")

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
            if order and order.deadline_valuation_price is not None:
                result += (required - filled) * order.deadline_valuation_price
            elif required > filled:
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
                if leg.payment_at == now and leg.final is not None:
                    self._record_payment(trade, leg, context)
                continue
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
        today = context.now.date()
        orders = self._orders(trade, "MANAGER")
        due_orders = [order for order in orders if order.pricing_deadline == today and order.remaining]
        if sub.final is not None or (not due_orders and sub.pricing_deadline != today):
            return
        at_close = datetime.combine(today, time(15))
        if context.now < at_close:
            return
        if context.now == at_close:
            # The deadline callback can precede the retry for the inclusive last minute.
            self._attempt_cross_market_substitution(trade, context, schedule_next=False)
        config = context.resources.config
        for order in due_orders:
            if not order.remaining:
                continue
            capped = order.deadline_basis == "exchange_20_day_cap"
            bar = (exception_close(order.symbol, today, config.trading_days, context.resources.rules, config)
                   if capped else self.prices.get(order.symbol, at_close, exact=True))
            if bar is None:
                trade.reason = "missing_substitution_deadline_close"
                continue
            self.substitution_pricing.append(dict(trade_id=trade.trade_id, symbol=order.symbol,
                quantity=order.remaining, price=bar.close, ts=at_close,
                basis="unfilled_quantity_at_last_actual_close" if capped else "unfilled_quantity_at_deadline_close",
                source_ts=bar.time))
            order.deadline_valuation_price = bar.close
            order.pricing_close_at = bar.time
            order.cancelled = True
            order.last_block = "valued_at_pricing_deadline_close"
        if sub.pricing_deadline == today and all(not order.remaining for order in orders):
            value = self._manager_value(trade, context.now)
            sub.estimate = (trade.plan.substitution_deposit - value if trade.decision.side is Side.CREATE
                            else value + trade.plan.domestic_fixed)
            self._confirm_substitution(trade, context, orders)
            self._settle(trade, context)

    def _confirm_substitution(self, trade, context, orders):
        sub = trade.legs[1]
        sub.final = sub.estimate
        sub.known_on, sub.priced_at = context.now.date(), context.now
        sub.pricing_basis = ("filled_proxy_values_plus_deadline_close" if any(
            o.deadline_valuation_price is not None for o in orders) else "completed_manager_close_proxy")
        if sub.payment_at is not None:
            if sub.payment_at < sub.priced_at:
                raise ValueError("Supplied substitution payment precedes final pricing")
            return
        lag = self.settings.cash_substitution_lag
        if lag is None:
            return
        days = context.resources.config.trading_days
        sub.due = trading_day_after(days, trade.applied_at.date(), lag)
        if self._uses_security_calendar() and orders:
            after_pricing = trading_day_after(days, sub.known_on, max(0, lag - 2))
            sub.due = max(sub.due, after_pricing) if sub.due and after_pricing else None
        sub.payment_basis = "explicit_research_lag"

    def _close_if_done(self, trade, context):
        if trade.state not in {"APPLIED", "UNWINDING"} or self.account.holdings(trade.trade_id):
            return
        if any(o.remaining for o in self._orders(trade)) or any(not leg.paid for leg in trade.legs):
            return
        unwound = trade.state == "UNWINDING"
        etf_orders = self._orders(trade, "ENTRY") if unwound else self._orders(trade)
        amount = sum((o.value for o in etf_orders if o.symbol == self.etf_symbol and o.purpose != "MANAGER"), ZERO)
        context.resources.portfolio.settle_trade(trade_id=trade.trade_id, settled_at=context.now,
            pnl=self.account.trade_cash.get(trade.trade_id, ZERO), amount=None if unwound and not amount else amount,
            include_in_etf_ledger=not unwound)
        trade.state = "UNWOUND" if unwound else "CLOSED"

    def on_time(self, context):
        if not self.trading_times.allows(context.now.time()):
            return
        for trade in self.trades:
            if trade.state in {"CLOSED", "HELD_PENDING_POLICY", "UNWOUND"}:
                continue
            if trade.state == "BUILD" and self.settings.assume_full_basket_fill:
                self._fill_full_basket(trade, context)
                continue  # Application has already attempted any configured same-minute exits.
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
