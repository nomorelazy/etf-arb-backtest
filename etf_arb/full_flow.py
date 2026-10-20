"""Composition root for a complete, explicitly labelled research simulation."""

import csv
import json
from dataclasses import asdict
from datetime import date, datetime
from decimal import Decimal

from etf_arb.data.snapshot import DailyCloseCache
from etf_arb.decision.premium import PremiumDiscountSource
from etf_arb.execution.simulator import ArbitrageSimulator
from etf_arb.report.render import save_report
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
    executor = ArbitrageSimulator(prices, settings)
    source = PremiumDiscountSource(prices, settings, executor.available)

    def report(result, resources):
        output = config.output_dir
        account = executor.account
        period = resources.portfolio.summarize_pnl(config.start_date, config.end_date)
        summary = dict(
            settlement_basis="research_proxy", opened_count=period.opened_count,
            settled_count=period.settled_count, pending_count=period.pending_count,
            settled_net_pnl=str(period.settled_net_pnl),
            final_net_pnl=None if period.final_net_pnl is None else str(period.final_net_pnl),
            ending_cash=str(account.cash), ending_equity=str(executor.equity_curve[-1].equity),
            marked_account_pnl=str(executor.equity_curve[-1].equity - config.starting_cash),
            fee="0", slippage="0", order_count=len(executor.orders),
            fill_count=sum(f["purpose"] != "MANAGER" for f in executor.fills),
            manager_proxy_fill_count=sum(f["purpose"] == "MANAGER" for f in executor.fills),
            rejected_decision_count=len(executor.rejections),
        )
        # Reconcile all actual model cash flows to the ledger and the starting balance.
        if account.cash - account.starting_cash != sum(account.trade_cash.values(), Decimal("0")):
            raise AssertionError("Account cash reconciliation failed")
        if not period.pending_count and not any(account.positions.values()):
            if account.cash - account.starting_cash != period.settled_net_pnl:
                raise AssertionError("Closed trade PnL reconciliation failed")
        assumptions = dict(
            model="159338 SZ physical / SH cash substitution; research simulation",
            premium_threshold=str(settings.premium_threshold), threshold_comparison="strict > +threshold / < -threshold",
            fees="0", slippage="0", fills="first strictly later continuous-auction minute with an exact valid close",
            liquidity="unlimited at each close unless max_shares_per_minute is configured; no volume data",
            security_rule_policy=settings.security_rule_policy,
            status_limitations="No historical bid/ask, volume, suspension master or adjusted daily price limits supplied. Missing bars never fill. Supplied dated security rules are enforced.",
            applications="Assume successful immediate confirmation once the full PCF consideration is available before cutoff; received assets may sell on a later minute",
            failed_basket="Cancel unfilled entry at day close; unwind purchased securities no earlier than the next supplied trading day",
            funding="No borrowing/shorting; positive estimated cash is funded upfront for both directions, refunded/adjusted with the actual cash component",
            cash_placeholder="159900 charged once as SH aggregate; SH disclosed rows are not investor physical positions",
            manager_execution="SH allowed substitution uses simulated manager trades on strictly later minute closes, zero costs; no actual fund-manager execution or refund records are available",
            substitution_payment=f"Aggregate net refund (CREATE) / proceeds (REDEEM) no earlier than application + {settings.cash_substitution_lag} supplied trading days and completion of all manager proxy orders. Provisional redemption cash is not made available early.",
            cash_difference=f"Subsequent PCF CashComponent matched only via PreTradingDay; payment {settings.cash_difference_lag} supplied trading days after publication. Tail horizon exhaustion leaves a pending leg.",
            scheduling="One active cycle at a time, including pending settlement; per-day cap is explicit simulation configuration",
            valuation="Cash + holdings at latest observed close + unsettled receivables/payables; stale holdings marks flagged in daily_account.csv",
            calendar="Explicit supplied calendar. See configuration/source notes; no weekday approximation.",
            settings=asdict(settings),
        )
        (output / "assumptions.json").write_text(json.dumps(assumptions, ensure_ascii=False, indent=2, default=_text), encoding="utf-8")
        (output / "financial_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        _csv(output / "signals.csv", source.rows, ["ts", "etf_close", "iopv", "premium", "side", "reason"])
        _csv(output / "orders.csv", [asdict(o) | {"remaining": o.remaining} for o in executor.orders],
             ["order_id", "trade_id", "symbol", "side", "quantity", "submitted", "purpose", "not_before_day", "filled", "remaining", "value", "cancelled", "last_block"])
        _csv(output / "fills.csv", executor.fills,
             ["ts", "order_id", "trade_id", "symbol", "side", "quantity", "price", "purpose", "fee", "slippage", "basis"])
        _csv(output / "cashflows.csv", account.rows,
             ["ts", "trade_id", "event", "cash_delta", "symbol", "quantity_delta", "cash_balance"])
        _csv(output / "cash_settlements.csv", [dict(trade_id=t.trade_id, **asdict(leg)) for t in executor.trades for leg in t.legs],
             ["trade_id", "kind", "estimate", "final", "known_on", "due", "paid"])
        _csv(output / "daily_account.csv", executor.daily,
             ["date", "cash", "receivables", "holdings_value", "equity", "stale_marks", "stale_cash_substitution"])
        _csv(output / "rejections.csv", executor.rejections, ["ts", "reason"])
        trade_by_id = {t.trade_id: t for t in executor.trades}
        _csv(output / "trades.csv", [dict(trade_id=t.trade_id, opened_at=t.opened_at, settled_at=t.settled_at,
             side=trade_by_id[t.trade_id].decision.side.value, state=trade_by_id[t.trade_id].state,
             quantity_units=trade_by_id[t.trade_id].decision.quantity_units,
             pcf_path=trade_by_id[t.trade_id].pcf.source_path,
             substitution_deposit=trade_by_id[t.trade_id].plan.substitution_deposit,
             domestic_fixed_cash=trade_by_id[t.trade_id].plan.domestic_fixed,
             estimated_cash=trade_by_id[t.trade_id].plan.estimated_cash,
             net_pnl=t.pnl, reason=trade_by_id[t.trade_id].reason) for t in resources.portfolio.trades],
             ["trade_id", "opened_at", "settled_at", "side", "quantity_units", "state", "net_pnl", "reason",
              "pcf_path", "substitution_deposit", "domestic_fixed_cash", "estimated_cash"])
        save_report(executor.equity_curve, resources.portfolio.to_closed_trades(), output / "report",
                    starting_cash=config.starting_cash,
                    title="159338 Research simulation (fees=0, slippage=0; proxy cash settlement)",
                    peak_cash_usage=max(Decimal("0"), max((config.starting_cash - row["cash_balance"] for row in account.rows), default=Decimal("0"))),
                    filled_orders=sum(o.purpose != "MANAGER" and o.filled == o.quantity for o in executor.orders),
                    rejected_orders=sum(o.cancelled for o in executor.orders),
                    failed_applications=sum(t.reason in {"basket_incomplete_at_close", "application_cutoff_or_disabled", "insufficient_application_cash"} for t in executor.trades))
        # Put the model basis and pending totals in the HTML itself, not just in sidecar files.
        path = output / "report" / "report.html"
        html = path.read_text(encoding="utf-8")
        notice = ("<aside style='padding:18px;background:#fff3cd;color:#332600'><strong>研究模拟：费用=0，滑点=0；现金替代按分钟行情模拟基金经理交易。</strong>"
                  f"<p>发起 {period.opened_count} 笔；完成 {period.settled_count} 笔；待完成 {period.pending_count} 笔。"
                  f"已完成净盈亏 {period.settled_net_pnl}；区间最终净盈亏 "
                  f"{period.final_net_pnl if period.final_net_pnl is not None else '待结算，暂不能确定'}。</p>"
                  "<p>下方曲线包含持仓及未结算款项的估值。基金经理实际交易和退补款记录尚未接入。"
                  "详见 <a href='../assumptions.json'>假设说明</a>。</p></aside>")
        start = html.find(">", html.find("<body")) + 1
        path.write_text(html[:start] + notice + html[start:], encoding="utf-8")
        return summary

    return BacktestRunner(config, source=source, executor=executor,
                          hooks=RunnerHooks(executor.on_day_open, executor.on_time, executor.on_day_close),
                          rules=rules, reporter=report)
