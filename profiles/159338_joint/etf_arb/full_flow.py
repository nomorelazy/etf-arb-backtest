"""Composition root for a complete, explicitly labelled research simulation."""

import csv
import json
from dataclasses import asdict
from datetime import date, datetime, timedelta
from decimal import Decimal

from etf_arb.data.adjusted_references import AdjustedReferences
from etf_arb.data.snapshot import DailyCloseCache
from etf_arb.decision.limit_loss import AUDIT_FIELDS
from etf_arb.decision.premium import PremiumDiscountSource
from etf_arb.rules.creation_feasibility import CreationBasketGuard
from etf_arb.execution.simulator import ArbitrageSimulator
from etf_arb.pricing.dynamic_cash import DynamicCashModel
from etf_arb.report.render import save_report
from etf_arb.report.prior_closes import PriorCloses, report_prior_path
from etf_arb.report.limit_value import build_limit_value, save_limit_value
from etf_arb.report.limit_exposure import build_from_saved_records, save_limit_exposure
from etf_arb.report.ledger import save_ledger_report
from etf_arb.report.attribution import build_trade_attribution, save_attribution_csv
from etf_arb.report.limit_impact import build_limit_impact, save_limit_impact
from etf_arb.report.signal_risk import build_signal_risk, save_signal_risk
from etf_arb.runner import BacktestRunner, RunnerConfigError, RunnerHooks
from etf_arb.rules.price_limit_filter import PriceLimitFilter


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
    previous_closes_path = report_prior_path(config)
    previous_closes = PriorCloses(DailyCloseCache(config.minute_root), previous_closes_path)
    adjusted = AdjustedReferences.from_config(config)
    etf_code, = config.etf_codes
    dynamic_cash = None
    if config.odd_lot_holdings_path is not None:
        dynamic_cash = DynamicCashModel.from_files(config.odd_lot_holdings_path,
                                                   config.odd_lot_reference_prices_path, etf_code)
    creation_guard = (CreationBasketGuard(
        prices, config.minute_root, bounds_path=config.daily_price_limits_path,
        previous_closes=previous_closes, adjusted_references=adjusted,
    ) if settings.check_creation_feasibility else None)
    execution_limits = (creation_guard.limits if creation_guard is not None else
                        PriceLimitFilter(prices, config.minute_root, config.daily_price_limits_path,
                                         previous_closes=previous_closes, adjusted_references=adjusted))
    limit_filter = execution_limits if config.filter_price_limits else None
    executor = ArbitrageSimulator(prices, settings, etf_code=etf_code, price_limit_filter=limit_filter,
                                  creation_guard=creation_guard, execution_limits=execution_limits)
    source = PremiumDiscountSource(prices, settings, executor.available, price_limit_filter=limit_filter,
                                   creation_guard=creation_guard, reference_limits=execution_limits,
                                   dynamic_cash=dynamic_cash)

    def day_open(context):
        signal_symbols = ()
        if dynamic_cash is not None and context.now.date() <= config.end_date:
            signal_symbols = dynamic_cash.required_symbols(context.pcfs[etf_code])
        executor.on_day_open(context, signal_symbols=signal_symbols)

    creation_threshold = settings.effective_creation_threshold
    redemption_threshold = settings.effective_redemption_threshold
    thresholds = dict(
        premium_threshold=str(creation_threshold) if creation_threshold == redemption_threshold else None,
        creation_threshold=str(creation_threshold),
        redemption_threshold=str(redemption_threshold),
    )

    def report(result, resources):
        output = config.output_dir
        account = executor.account
        fee_totals = {name: sum((row[name] for row in executor.fee_rows), Decimal("0"))
                      for name in ("commission", "stamp_duty", "transfer_fee", "handling_fee", "total")}
        investor_fees = sum((row["total"] for row in executor.fee_rows if row["charged_to"] == "investor_cash"), Decimal("0"))
        manager_fees = fee_totals["total"] - investor_fees
        period = resources.portfolio.summarize_pnl(config.start_date, config.end_date)
        individual_calendar = config.etf_codes[0] == "159338" and settings.cash_substitution_pricing_days == 2
        pricing_note = ('159338 的允许现金替代逐股计日。申请日后，证券正常交易的第二个交易日收盘时，已成交部分用原成交价及费用，剩余部分用当日收盘价估值。自申请日算起满 20 个交易日仍不足两个个股正常交易日，余量取最近一次实际收盘价。估值不收成交税费。'
                        if individual_calendar else
                        f"本轮按配置的交易所交易日历确定替代期限，申请日后第 {settings.cash_substitution_pricing_days} 个交易日到期。")
        summary = dict(
            individual_substitution_calendar=individual_calendar,
            substitution_pricing_note=pricing_note,
            optional_creation_deposit_note=('159338 本市场可选替代的申购预收款＝股数×除权除息调整后的前收盘价×（1＋PCF 溢价比例）。预收款与最后退补相抵。'
                                            if config.etf_codes[0] == "159338" else ""),
            etf_code=etf_code,
            settle_estimated_cash_at_application=settings.settle_estimated_cash_at_application,
            check_creation_feasibility=settings.check_creation_feasibility,
            creation_cash_ratio_block_count=sum(row["reason"] == "creation_cash_ratio_exceeded" for row in source.rows),
            creation_feasibility_block_count=sum(row["side"] == "CREATE" and row["reason"] in {
                "creation_cash_ratio_exceeded", "unbuyable_and_no_cash_substitution",
                "missing_cash_substitution_data"} for row in source.rows),
            **thresholds,
            trade_direction=settings.trade_direction,
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
        if dynamic_cash is not None:
            unpriced = [r for r in source.rows if r['reason'].startswith('dynamic_unpriced:')]
            summary.update(
                pricing_mode="dynamic_odd_lot_cash",
                dynamic_unpriced_minutes=len(unpriced),
                dynamic_unpriced_dates=sorted({r['ts'].date().isoformat() for r in unpriced}),
            )
        if settings.limit_loss_threshold:
            summary.update(
                limit_loss_threshold=True,
                limit_loss_calibration_mode="fixed_full_sample",
                limit_loss_blocks=sum(row["reason"] == "below_limit_loss_threshold" for row in source.rows),
                limit_loss_incomplete_candidates=sum(bool(row.get("limit_loss_unknown_count")) for row in source.rows),
            )
            if dynamic_cash is not None:
                summary["strategy_mode"] = "dynamic_odd_lot_cash_with_limit_loss"
        if limit_filter is not None:
            blocked_signals = [row for row in source.rows
                               if row["reason"] in {"price_limit_touched", "price_limit_unknown"}]
            summary.update(
                filter_price_limits=True,
                price_limit_signal_blocks=sum(row["reason"] == "price_limit_touched" for row in blocked_signals),
                price_limit_unknown_signal_blocks=sum(row["reason"] == "price_limit_unknown" for row in blocked_signals),
                price_limit_application_blocks=len(executor.price_limit_application_blocks),
                submitted_creation_count=sum(t.applied_at is not None and t.decision.side.value == "CREATE"
                                             for t in executor.trades),
                submitted_redemption_count=sum(t.applied_at is not None and t.decision.side.value == "REDEEM"
                                               for t in executor.trades),
            )
            _csv(output / "price_limit_blocks.csv",
                 [dict(ts=row["ts"], stage="signal", side=row["side"], reason=row["reason"],
                       component_reasons=row["component_reasons"]) for row in blocked_signals]
                 + [dict(row, stage="application") for row in executor.price_limit_application_blocks],
                 ["ts", "stage", "trade_id", "side", "reason", "component_reasons"])
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
            execution_prior_closes_path=str(previous_closes_path) if previous_closes_path else None,
            execution_prior_closes_sha256=previous_closes.sha256,
            adjusted_references_sha256=adjusted.sha256,
            suspension_pricing_basis=pricing_note,
            stock_suspensions_sha256=getattr(getattr(resources.rules, "suspensions", None), "sha256", None),
            price_limit_reference_priority="Dated suspension notices govern tradability. For tradable stocks use provided daily bounds, then a sourced effective-day adjusted reference, the exact local PCF previous close, a sourced close on the same prior day, or the exact 09:30 proxy.",
            creation_feasibility_check=settings.check_creation_feasibility,
            creation_cash_ratio="Optional domestic substituted shares at supplied adjusted references or exact labelled previous closes / (creation unit * ETF reference); excludes premium, mandatory cash, cross-market proxy and estimated cash; missing required data blocks creation",
            creation_buyability="Minute close touching the upper limit is conservatively unbuyable; official limits override previous-close estimates; lower limit alone does not block BUY",
            trade_direction=settings.trade_direction,
            model=f"{etf_code} research simulation: domestic physical basket and cross-market cash substitution",
            market_split_execution=settings.market_split_execution,
            entry_at_signal_time=settings.entry_at_signal_time,
            creation_etf_sale_at_application=settings.entry_at_signal_time and settings.market_split_execution,
            entry_timing=("Signal-time entry: redemption ETF BUY and creation physical stock BUY use the exact signal t close. Application stays on the next permitted minute (t+1 in market-split mode), and normal redemption stock sales use that application minute. " + creation_exit_policy + " Signal-close execution is an explicit user research assumption."
                          if settings.entry_at_signal_time else "Legacy entry: investor BUY orders use the next permitted exact close after the signal, with application after the complete basket is acquired."),
            **thresholds, threshold_comparison=(">= +creation_threshold / <= -redemption_threshold" if dynamic_cash is not None
                                               else "strict > +creation_threshold / < -redemption_threshold"),
            fees=asdict(settings.fees), fee_policy="User-specified rates. No minimum commission. Round each fee component per actual fill to cents, half up. Investor fees are separate cash events; manager fees enter substitution refunds/payables once. No execution fees on unfilled close valuations, security transfers or fixed PCF substitution amounts. SecurityTradingRule fee metadata is not separately charged.",
            signal_cost_policy="Estimate fees at currently observed prices, preserving the raw premium/discount trigger; estimated net edge is reported without adding an unrequested net-profit gate. Manager pending-value marks remain gross until actual proxy fills determine incurred fees.",
            slippage="0", fills=fill_policy,
            liquidity="Own orders and cross-market first-tradable-minute proxies fill in full at an exact permitted close. max_shares_per_minute applies to scheduled optional replacements (all scheduled proxies in legacy mode). No volume data.",
            security_rule_policy=settings.security_rule_policy,
            creation_signal_gate="Positive-quantity real constituents that cannot be bought and forbid cash substitution block CREATE before signal emission. Zero-quantity rows and the aggregate cash placeholder do not block. REDEEM is unchanged.",
            optional_creation_substitution=(summary["optional_creation_deposit_note"] or "When an allowed domestic constituent cannot be bought at signal time, the research deposit is current exact minute close * shares * (1 + PCF creation premium). Missing close/premium leaves the creation unavailable. The deposit is funded once and adjusted after pricing."),
            status_limitations="Historical bid/ask and volume are unavailable. Dated suspension notices block stock fills. Supplied adjusted references and daily limits are used when present. Missing bars never fill.",
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
                if settings.market_split_execution else "Legacy research policy: cash-substitution proxies wait for the scheduled attempt. A cross-market redemption sale blocked there by the lower limit retries each eligible minute through the pricing deadline. "
            ) + f"Scheduled attempt: on the applicable pricing deadline at {settings.cash_substitution_attempt_time}, with the configured quantity cap. Final substitution cash is confirmed on the applicable pricing deadline; earlier filled quantities keep their execution prices and fees. Pricing and cash receipt remain separate.",
            redemption_limit_down_policy="On a recognized lower-limit minute close, redeemed domestic stock remains in holdings. Both physical and cross-market sales retry each exchange minute, independently of the arbitrage signal clock and opening sessions. Only unfilled proxy shares are valued at the deadline, without a fill or fees.",
            substitution_pricing=pricing_note,
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
        extra_signal_columns = []
        if dynamic_cash is not None:
            extra_signal_columns.extend(["pcf_cash_per_unit", "cash_adjustment_per_unit", "signal_cash_per_unit",
                                         "security_value_per_unit", "fixed_cash_per_unit"])
            assumptions.update(
                pricing_mode="dynamic_odd_lot_cash", odd_lot_holdings_path=str(config.odd_lot_holdings_path),
                odd_lot_reference_prices_path=str(config.odd_lot_reference_prices_path),
                signal_cash_basis="Per CU, PCF estimated cash plus effective shares times the change from the adjusted reference price to the signal minute. The settlement uses the original PCF cash terms.",
            )
        if settings.limit_loss_threshold:
            extra_signal_columns.extend(AUDIT_FIELDS)
            assumptions.update(
                limit_loss_threshold=True,
                limit_loss_calibration_mode=summary["limit_loss_calibration_mode"],
                limit_loss_input=("159338 双 0.17% 逐股样本保留同日多笔，剔除 2025-04-07。"
                                  "全段固定使用带符号的纯价差盈亏与市值校准的分档系数，校准期重放属于样本内对照。"),
                limit_loss_unknown_policy="Only confirmed directional hits contribute to estimated loss. Unknowns are recorded.",
                threshold_comparison=("Strict directional premium/discount magnitude > base threshold + estimated "
                                      "directional stock loss / (signal IOPV times ETF shares in the order). "
                                      "The loss estimate affects the signal threshold only."),
            )
            if dynamic_cash is not None:
                assumptions["strategy_mode"] = summary["strategy_mode"]
        (output / "assumptions.json").write_text(json.dumps(assumptions, ensure_ascii=False, indent=2, default=_text), encoding="utf-8")
        (output / "financial_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        _csv(output / "signals.csv", source.rows,
             ["ts", "etf_close", "iopv", "premium", "side", "reason", "blocked_components", "component_reasons", "cash_substituted_components", "estimated_fee", "estimated_net_edge",
              "cash_substitution_ratio", "max_cash_substitution_ratio",
              "cash_substitution_reference_value", "cash_substitution_reference_basis"] + extra_signal_columns)
        _csv(output / "fees.csv", executor.fee_rows,
             ["ts", "trade_id", "order_id", "symbol", "side", "purpose", "turnover", "commission", "stamp_duty", "transfer_fee", "handling_fee", "total", "charged_to"])
        _csv(output / "creation_substitutions.csv", executor.creation_substitutions,
             ["ts", "trade_id", "symbol", "quantity", "price", "premium_rate", "deposit", "reason", "basis", "observed_price", "observation_stage"])
        _csv(output / "orders.csv", [asdict(o) | {"remaining": o.remaining, "last_sh_attempted_at": o.last_sh_attempted_at}
                                     for o in executor.orders],
             ["order_id", "trade_id", "symbol", "side", "quantity", "submitted", "purpose", "not_before_day", "filled", "remaining", "value", "fee", "cancelled", "paused", "manager_attempted_at", "next_minute_attempted_at", "last_sh_attempted_at", "last_cross_market_attempted_at", "last_block",
              "pricing_deadline", "deadline_basis", "attempt_scheduled_at", "pricing_close_at", "deadline_valuation_price"])
        _csv(output / "fills.csv", executor.fills,
             ["ts", "order_id", "trade_id", "symbol", "side", "quantity", "price", "purpose", "fee", "commission", "stamp_duty", "transfer_fee", "handling_fee", "slippage", "basis"])
        _csv(output / "cashflows.csv", account.rows,
             ["ts", "trade_id", "event", "cash_delta", "symbol", "quantity_delta", "cash_balance"])
        _csv(output / "cash_settlements.csv", [dict(trade_id=t.trade_id, **asdict(leg)) for t in executor.trades for leg in t.legs],
             ["trade_id", "kind", "estimate", "final", "known_on", "pricing_deadline", "attempt_scheduled_at", "priced_at", "notice_due_on", "latest_payment_on", "due", "payment_at", "paid_at", "paid", "pricing_basis", "payment_basis", "actual_component"])
        _csv(output / "substitution_pricing.csv", executor.substitution_pricing,
             ["trade_id", "symbol", "quantity", "price", "ts", "basis", "source_ts"])
        _csv(output / "execution_limit_blocks.csv", executor.execution_limit_blocks,
             ["ts", "trade_id", "order_id", "symbol", "side", "purpose", "unfilled_quantity", "close", "reason"])
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
        if dynamic_cash is not None:
            for row in trade_rows:
                decision = trade_by_id[row["trade_id"]].decision
                row["signal_cash_estimate"] = decision.signal_cash_per_unit * decision.quantity_units
                row["signal_cash_adjustment"] = row["signal_cash_estimate"] - row["estimated_cash"]
        _csv(output / "trades.csv", trade_rows,
             ["trade_id", "opened_at", "applied_at", "redemption_prices_locked_at", "delayed_redemption_components", "next_signal_not_before", "settled_at", "side", "quantity_units", "etf_shares", "etf_leg_amount", "state", "net_pnl", "reason",
              "etf_symbol", "pcf_path", "substitution_deposit", "domestic_fixed_cash", "estimated_cash",
              "signal_gross_edge", "signal_physical_value", "signal_manager_value", "signal_fixed_cash", "signal_physical_quantity"]
             + (["signal_cash_estimate", "signal_cash_adjustment"] if dynamic_cash is not None else []))
        resources.portfolio.export_csv(output / "portfolio_ledger.csv")
        settlement_rows = [dict(trade_id=t.trade_id, **asdict(leg)) for t in executor.trades for leg in t.legs]
        attribution_rows = build_trade_attribution(trade_rows, source.rows, executor.fills,
            settlement_rows, executor.fee_rows, executor.substitution_pricing)
        save_attribution_csv(output / "trade_attribution.csv", attribution_rows, dynamic=dynamic_cash is not None)
        limit_impact = build_limit_impact(config, trade_rows, source.rows, executor.fills,
            executor.fee_rows, executor.substitution_pricing, executor.creation_substitutions,
            settlement_rows, executor.substitution_attempts, orders=(vars(o) for o in executor.orders), rules=resources.rules,
            application_blocks=getattr(executor, "price_limit_application_blocks", ()))
        save_limit_impact(output, limit_impact)
        signal_risk = build_signal_risk(config, trade_rows, source.rows, rules=resources.rules)
        save_signal_risk(output, signal_risk)
        limit_value = build_limit_value(config, trade_rows, source.rows, rules=resources.rules)
        save_limit_value(output, limit_value)
        limit_exposure = build_from_saved_records(
            config, trade_rows, [asdict(order) for order in executor.orders], executor.fills,
            executor.fee_rows, executor.substitution_pricing, source.rows,
            substitutions=executor.creation_substitutions, attempts=executor.substitution_attempts,
            settlements=settlement_rows, execution_blocks=executor.execution_limit_blocks,
            stock_rows=limit_impact["rows"], rules=resources.rules)
        save_limit_exposure(output, limit_exposure)
        save_ledger_report(output / "report/ledger.html", trade_rows, account.rows,
                           executor.fills, settlement_rows, executor.substitution_pricing,
                           executor.substitution_attempts, executor.creation_substitutions, executor.fee_rows,
                           attribution_rows=attribution_rows, signal_risk=signal_risk, limit_impact=limit_impact)
        save_report(executor.equity_curve, resources.portfolio.to_closed_trades(), output / "report",
                    starting_cash=config.starting_cash, limit_value=limit_value,
                    trade_rows=resources.portfolio.to_rows(), financial_summary=summary,
                    attribution_rows=attribution_rows, signal_risk=signal_risk, limit_impact=limit_impact,
                    limit_exposure=limit_exposure,
                    total_costs=fee_totals["total"],
                    title=f"{etf_code} Research simulation (configured fees, slippage=0; proxy cash settlement)",
                    peak_cash_usage=max(Decimal("0"), max((config.starting_cash - row["cash_balance"] for row in account.rows), default=Decimal("0"))),
                    filled_orders=sum(o.purpose != "MANAGER" and o.filled == o.quantity for o in executor.orders),
                    rejected_orders=sum(o.cancelled and o.purpose != "MANAGER" for o in executor.orders),
                    failed_applications=(len(executor.price_limit_application_blocks)
                                         + sum(t.reason in {"basket_incomplete_at_close", "application_cutoff_or_disabled",
                                                            "insufficient_application_cash"} for t in executor.trades)))
        # report.html is owned by the standard renderer; do not rewrite its template.
        return summary

    return BacktestRunner(config, source=source, executor=executor,
                          hooks=RunnerHooks(day_open, executor.on_time, executor.on_day_close),
                          rules=rules, reporter=report)
