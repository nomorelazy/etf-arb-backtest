"""Composition root for a complete, explicitly labelled research simulation."""

import csv
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from etf_arb.data.snapshot import DailyCloseCache
from etf_arb.decision.premium import PremiumDiscountSource
from etf_arb.execution.simulator import ArbitrageSimulator
from etf_arb.report.render import save_report
from etf_arb.report.ledger import save_ledger_report
from etf_arb.report.attribution import build_trade_attribution, save_attribution_csv
from etf_arb.runner import BacktestRunner, RunnerConfigError, RunnerHooks


def _text(value):
    return value.isoformat() if isinstance(value, (date, datetime)) else str(value)


def _csv(path, rows, columns):
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def make_full_runner(config, *, rules=None):
    settings = config.simulation
    if settings is None or config.mode != "execute":
        raise RunnerConfigError("Full-flow runner requires mode=execute and simulation settings")
    prices = DailyCloseCache(config.minute_root)
    etf_code, = config.etf_codes
    executor = ArbitrageSimulator(prices, settings, etf_code=etf_code)
    source = PremiumDiscountSource(prices, settings, executor.available)

    def report(result, resources):
        output = config.output_dir
        account = executor.account
        fee_totals = {name: sum((row[name] for row in executor.fee_rows), Decimal("0"))
                      for name in ("commission", "stamp_duty", "transfer_fee", "handling_fee", "total")}
        investor_fees = sum((row["total"] for row in executor.fee_rows if row["charged_to"] == "investor_cash"), Decimal("0"))
        manager_fees = fee_totals["total"] - investor_fees
        period = resources.portfolio.summarize_pnl(config.start_date, config.end_date)
        summary = dict(
            etf_code=etf_code,
            settle_estimated_cash_at_application=settings.settle_estimated_cash_at_application,
            premium_threshold=str(settings.premium_threshold),
            blocked_trade_minutes=list(settings.blocked_trade_minutes),
            trading_sessions=[list(pair) for pair in settings.trading_sessions],
            settlement_basis="research_proxy", opened_count=period.opened_count,
            creation_count=sum(t.decision.side.value == 'CREATE' for t in executor.trades),
            redemption_count=sum(t.decision.side.value == 'REDEEM' for t in executor.trades),
            settled_count=period.settled_count, pending_count=period.pending_count,
            settled_net_pnl=str(period.settled_net_pnl),
            final_net_pnl=None if period.final_net_pnl is None else str(period.final_net_pnl),
            ending_cash=str(account.cash), ending_equity=str(executor.equity_curve[-1].equity),
            marked_account_pnl=str(executor.equity_curve[-1].equity - config.starting_cash),
            fee=str(fee_totals["total"]), fee_breakdown={name: str(value) for name, value in fee_totals.items()},
            investor_fees=str(investor_fees), manager_fees=str(manager_fees),
            slippage="0", order_count=len(executor.orders),
            fill_count=sum(f["purpose"] != "MANAGER" for f in executor.fills),
            manager_proxy_fill_count=sum(f["purpose"] == "MANAGER" for f in executor.fills),
            rejected_decision_count=len(executor.rejections),
            held_pending_policy_count=sum(t.state == "HELD_PENDING_POLICY" for t in executor.trades),
            assume_full_basket_fill=settings.assume_full_basket_fill,
            market_split_execution=settings.market_split_execution,
            entry_at_signal_time=settings.entry_at_signal_time,
            creation_etf_sale_at_application=settings.entry_at_signal_time and settings.market_split_execution,
            redemption_price_locked_count=sum(t.redemption_prices_locked_at is not None for t in executor.trades),
            delayed_redemption_count=sum(bool(t.delayed_redemption) for t in executor.trades),
            priced_unpaid_cash_legs=sum(leg.final is not None and not leg.paid for t in executor.trades for leg in t.legs),
            frequency_anchor="application_submitted_at",
            min_trade_interval_minutes=settings.min_trade_interval_minutes,
            peak_open_cycles=executor.peak_open_cycles,
        )
        # Reconcile all actual model cash flows to the ledger and the starting balance.
        if account.cash - account.starting_cash != sum(account.trade_cash.values(), Decimal("0")):
            raise AssertionError("Account cash reconciliation failed")
        fee_events = {"COMMISSION_PAID", "STAMP_DUTY_PAID", "TRANSFER_FEE_PAID", "HANDLING_FEE_PAID"}
        if investor_fees != -sum((row["cash_delta"] for row in account.rows if row["event"] in fee_events), Decimal("0")):
            raise AssertionError("Investor fee cash reconciliation failed")
        if not period.pending_count and not any(account.positions.values()):
            if account.cash - account.starting_cash != period.settled_net_pnl:
                raise AssertionError("Closed trade PnL reconciliation failed")
        creation_exit_policy = ("Creation ETF sells in the t+1 application minute at an exact permitted close."
                                if settings.entry_at_signal_time and settings.market_split_execution
                                else "Creation ETF exits wait for a later permitted minute after application.")
        if settings.market_split_execution:
            fill_policy = ("Entry BUY uses signal t exact permitted close; applications still wait for t+1. "
                           if settings.entry_at_signal_time else "Entry uses signal t+1 exact permitted close. ")
            fill_policy += ("Physical redemption exits use the application minute. Cross-market proxies use the first tradable exact minute "
                            "from application through pricing-deadline 15:00 inclusive. " + creation_exit_policy)
        else:
            fill_policy = ("Entry BUY uses signal t exact close; later application and exits retain their configured timing."
                           if settings.entry_at_signal_time else "first strictly later permitted trading minute with an exact valid close")
        assumptions = dict(
            model=f"{etf_code} research simulation: domestic physical basket and cross-market cash substitution",
            market_split_execution=settings.market_split_execution,
            entry_at_signal_time=settings.entry_at_signal_time,
            creation_etf_sale_at_application=settings.entry_at_signal_time and settings.market_split_execution,
            entry_timing=("Signal-time entry: redemption ETF BUY and creation physical stock BUY use the exact signal t close. Application stays on the next permitted minute (t+1 in market-split mode), and normal redemption stock sales use that application minute. " + creation_exit_policy + " Signal-close execution is an explicit user research assumption."
                          if settings.entry_at_signal_time else "Legacy entry: investor BUY orders use the next permitted exact close after the signal, with application after the complete basket is acquired."),
            premium_threshold=str(settings.premium_threshold), threshold_comparison="strict > +threshold / < -threshold",
            fees=asdict(settings.fees), fee_policy="User-specified rates. No minimum commission. Round each fee component per actual fill to cents, half up. Investor fees are separate cash events; manager fees enter substitution refunds/payables once. No execution fees on unfilled close valuations, security transfers or fixed PCF substitution amounts. SecurityTradingRule fee metadata is not separately charged.",
            signal_cost_policy="Estimate fees at currently observed prices, preserving the raw premium/discount trigger; estimated net edge is reported without adding an unrequested net-profit gate. Manager pending-value marks remain gross until actual proxy fills determine incurred fees.",
            slippage="0", fills=fill_policy,
            liquidity="Own orders and cross-market first-tradable-minute proxies fill in full at an exact permitted close. max_shares_per_minute applies to scheduled optional replacements (all scheduled proxies in legacy mode). No volume data.",
            security_rule_policy=settings.security_rule_policy,
            creation_signal_gate="Positive-quantity real constituents that cannot be bought and forbid cash substitution block CREATE before signal emission. Zero-quantity rows and the aggregate cash placeholder do not block. REDEEM is unchanged.",
            optional_creation_substitution="When an allowed domestic constituent cannot be bought at signal time, the research deposit is current exact minute close * shares * (1 + PCF creation premium). Missing close/premium leaves the creation unavailable. The deposit is funded once and adjusted after pricing.",
            status_limitations="No historical bid/ask, volume, suspension master or adjusted daily price limits supplied. Missing bars never fill. Supplied dated security rules are enforced.",
            applications=("Assume immediate confirmation once full PCF consideration is available before cutoff. Domestic physical redemption stocks sell in the t+1 application minute when tradable; cross-market proxies use the first tradable minute through the deadline. " + creation_exit_policy if settings.market_split_execution else "Assume successful immediate confirmation once the full PCF consideration is available before cutoff; received assets may sell on a later minute"),
            failed_basket=("User assumes all baskets can be completed. Preflight all entry quotes, rules, application permission and full cash before any leg fills; own orders are not size-capped or reduced to affordable partial lots. Contradictory inputs fail the run, without creating partial holdings or HELD_PENDING_POLICY." if settings.assume_full_basket_fill else "Explicit legacy partial-basket experiment: keep acquired securities and the unfinished cycle; pause remaining entry orders pending an explicit disposition policy. No automatic unwind or next-day application with yesterday's PCF."),
            funding=("No borrowing/shorting. Signed estimated cash is exchanged at application, then adjusted with the T+1 PCF's actual amount."
                     if settings.settle_estimated_cash_at_application else
                     "No borrowing/shorting. Positive estimated cash is funded upfront for both directions and adjusted with the actual amount."),
            cash_placeholder=("159900 used once as the aggregate cross-market deposit for SZ-listed ETFs. "
                              "SH-listed creation deposits use the PCF cross-market reference amounts and premiums."),
            manager_execution=(
                "Market-split research policy: cross-market creation BUY / redemption SELL proxies retry every minute from actual application through pricing-deadline 15:00 inclusive, filling at the first permitted exact close. The manager clock is 09:30-11:30 and 13:00-15:00 inclusive. Unfilled quantities use the exact deadline close for valuation. "
                f"{executor.etf_symbol[-2:]} optional creation replacements always wait for the scheduled attempt. Unsellable physical redemption stocks retain holdings and retry on later permitted minutes. "
                if settings.market_split_execution else "Legacy research policy: all cash-substitution proxies wait for the scheduled attempt. "
            ) + f"Scheduled attempt: application + {settings.cash_substitution_pricing_days} supplied trading days at {settings.cash_substitution_attempt_time}, with the configured quantity cap. Final substitution cash is confirmed on the original pricing deadline; earlier filled quantities keep their execution prices and fees. Pricing and cash receipt remain separate.",
            substitution_pricing=f"Ordinary research case: at most {settings.cash_substitution_pricing_days} supplied trading days after application; unfilled proxy quantities valued only at the exact deadline-day 15:00 close. This is valuation, not an investor fill. Missing deadline close leaves pricing unresolved. Individual suspended-day extensions, corporate actions and the 20-day special case require additional historical inputs.",
            substitution_payment="Pricing, notice and payment are separate. Explicit business calendar determines notice/bound dates; only a supplied payment timestamp credits cash. Missing payment date stays unpaid. An explicitly configured non-null cash_substitution_lag is a research override, not a prospectus payment guarantee.",
            cash_difference="Actual cash uses SZ CashComponent or SH PreCashComponent matched via PreTradingDay. Signed-estimate mode requires the next trading day's PCF. Default payment requires a supplied timestamp; a non-null cash_difference_lag is an explicit research override.",
            scheduling=f"Overlapping cycles are allowed, including unpaid cash settlements. A new signal may open a cycle after {settings.min_trade_interval_minutes} elapsed minutes from the latest actual creation/redemption submission (applied_at), subject to cash, basket and trading-session checks. A not-yet-submitted entry waits for its application timestamp before the clock starts. Daily cap={settings.max_trades_per_day} (None means no daily cap). CREATE and REDEEM share the quota; stock orders do not consume it. No intraday reset; lunch counts as elapsed time.",
            valuation="Cash + holdings at latest observed close + unsettled receivables/payables; stale holdings marks flagged in daily_account.csv",
            calendar="Explicit supplied calendar. See configuration/source notes; no weekday approximation.",
            settings=asdict(settings),
            blocked_trade_minutes=list(settings.blocked_trade_minutes),
            trading_sessions=[list(pair) for pair in settings.trading_sessions],
            blocked_minute_policy="Investor signals, applications and fills follow configured windows. Cross-market manager proxies use their independent minute-close clock and dated security rules. Exact cash-payment callbacks remain independent.",
        )
        (output / "assumptions.json").write_text(json.dumps(assumptions, ensure_ascii=False, indent=2, default=_text), encoding="utf-8")
        (output / "financial_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        _csv(output / "signals.csv", source.rows,
             ["ts", "etf_close", "iopv", "premium", "side", "reason", "blocked_components", "component_reasons", "cash_substituted_components", "estimated_fee", "estimated_net_edge"])
        _csv(output / "fees.csv", executor.fee_rows,
             ["ts", "trade_id", "order_id", "symbol", "side", "purpose", "turnover", "commission", "stamp_duty", "transfer_fee", "handling_fee", "total", "charged_to"])
        _csv(output / "creation_substitutions.csv", executor.creation_substitutions,
             ["ts", "trade_id", "symbol", "quantity", "price", "premium_rate", "deposit", "reason", "basis"])
        _csv(output / "orders.csv", [asdict(o) | {"remaining": o.remaining, "last_sh_attempted_at": o.last_sh_attempted_at}
                                     for o in executor.orders],
             ["order_id", "trade_id", "symbol", "side", "quantity", "submitted", "purpose", "not_before_day", "filled", "remaining", "value", "fee", "cancelled", "paused", "manager_attempted_at", "next_minute_attempted_at", "last_sh_attempted_at", "last_cross_market_attempted_at", "last_block"])
        _csv(output / "fills.csv", executor.fills,
             ["ts", "order_id", "trade_id", "symbol", "side", "quantity", "price", "purpose", "fee", "commission", "stamp_duty", "transfer_fee", "handling_fee", "slippage", "basis"])
        _csv(output / "cashflows.csv", account.rows,
             ["ts", "trade_id", "event", "cash_delta", "symbol", "quantity_delta", "cash_balance"])
        _csv(output / "cash_settlements.csv", [dict(trade_id=t.trade_id, **asdict(leg)) for t in executor.trades for leg in t.legs],
             ["trade_id", "kind", "estimate", "final", "known_on", "pricing_deadline", "attempt_scheduled_at", "priced_at", "notice_due_on", "latest_payment_on", "due", "payment_at", "paid_at", "paid", "pricing_basis", "payment_basis", "actual_component"])
        _csv(output / "substitution_pricing.csv", executor.substitution_pricing,
             ["trade_id", "symbol", "quantity", "price", "ts", "basis"])
        _csv(output / "substitution_attempts.csv", executor.substitution_attempts,
             ["trade_id", "order_id", "symbol", "ts", "side", "requested_quantity", "filled_quantity", "unfilled_quantity", "result", "reason", "timing"])
        _csv(output / "daily_account.csv", executor.daily,
             ["date", "cash", "receivables", "holdings_value", "equity", "stale_marks", "stale_cash_substitution"])
        _csv(output / "rejections.csv", executor.rejections, ["ts", "reason"])
        trade_by_id = {t.trade_id: t for t in executor.trades}
        trade_rows = [dict(trade_id=t.trade_id, opened_at=t.opened_at, settled_at=t.settled_at,
             applied_at=trade_by_id[t.trade_id].applied_at,
             redemption_prices_locked_at=trade_by_id[t.trade_id].redemption_prices_locked_at,
             delayed_redemption_components=json.dumps(trade_by_id[t.trade_id].delayed_redemption, ensure_ascii=False),
             next_signal_not_before=(trade_by_id[t.trade_id].applied_at + timedelta(minutes=settings.min_trade_interval_minutes)
                                     if trade_by_id[t.trade_id].applied_at else None),
             side=trade_by_id[t.trade_id].decision.side.value, state=trade_by_id[t.trade_id].state,
             quantity_units=trade_by_id[t.trade_id].decision.quantity_units,
             etf_shares=trade_by_id[t.trade_id].plan.etf_shares,
             etf_symbol=executor.etf_symbol,
             etf_leg_amount=t.amount,
             pcf_path=trade_by_id[t.trade_id].pcf.source_path,
             substitution_deposit=trade_by_id[t.trade_id].plan.substitution_deposit,
             domestic_fixed_cash=trade_by_id[t.trade_id].plan.domestic_fixed,
             estimated_cash=trade_by_id[t.trade_id].plan.estimated_cash,
             signal_gross_edge=trade_by_id[t.trade_id].decision.gross_edge,
             **trade_by_id[t.trade_id].signal_reference,
             net_pnl=t.pnl, reason=trade_by_id[t.trade_id].reason) for t in resources.portfolio.trades]
        _csv(output / "trades.csv", trade_rows,
             ["trade_id", "opened_at", "applied_at", "redemption_prices_locked_at", "delayed_redemption_components", "next_signal_not_before", "settled_at", "side", "quantity_units", "etf_shares", "etf_leg_amount", "state", "net_pnl", "reason",
              "etf_symbol", "pcf_path", "substitution_deposit", "domestic_fixed_cash", "estimated_cash",
              "signal_gross_edge", "signal_physical_value", "signal_manager_value", "signal_fixed_cash", "signal_physical_quantity"])
        resources.portfolio.export_csv(output / "portfolio_ledger.csv")
        settlement_rows = [dict(trade_id=t.trade_id, **asdict(leg)) for t in executor.trades for leg in t.legs]
        attribution_rows = build_trade_attribution(trade_rows, source.rows, executor.fills,
            settlement_rows, executor.fee_rows, executor.substitution_pricing)
        save_attribution_csv(output / "trade_attribution.csv", attribution_rows)
        save_ledger_report(output / "report/ledger.html", trade_rows, account.rows,
                           executor.fills, settlement_rows, executor.substitution_pricing,
                           executor.substitution_attempts, executor.creation_substitutions, executor.fee_rows,
                           attribution_rows=attribution_rows)
        save_report(executor.equity_curve, resources.portfolio.to_closed_trades(), output / "report",
                    starting_cash=config.starting_cash,
                    trade_rows=resources.portfolio.to_rows(), financial_summary=summary,
                    attribution_rows=attribution_rows,
                    total_costs=fee_totals["total"],
                    title=f"{etf_code} Research simulation (configured fees, slippage=0; proxy cash settlement)",
                    peak_cash_usage=max(Decimal("0"), max((config.starting_cash - row["cash_balance"] for row in account.rows), default=Decimal("0"))),
                    filled_orders=sum(o.purpose != "MANAGER" and o.filled == o.quantity for o in executor.orders),
                    rejected_orders=sum(o.cancelled and o.purpose != "MANAGER" for o in executor.orders),
                    failed_applications=sum(t.reason in {"basket_incomplete_at_close", "application_cutoff_or_disabled", "insufficient_application_cash"} for t in executor.trades))
        # report.html is owned by the standard renderer; do not rewrite its template.
        return summary

    return BacktestRunner(config, source=source, executor=executor,
                          hooks=RunnerHooks(executor.on_day_open, executor.on_time, executor.on_day_close),
                          rules=rules, reporter=report)
