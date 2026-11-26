"""Reserve one reports directory for each full-flow invocation."""

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from etf_arb.runner_config import RunnerConfig


def create_report_run(
    config: RunnerConfig, etf_codes: tuple[str, ...], reports_root: Path, label: str,
) -> tuple[str, Path, tuple[RunnerConfig, ...]]:
    if config.simulation is None:
        raise ValueError("Selecting ETFs requires a full-flow simulation configuration")
    if not etf_codes or len(set(etf_codes)) != len(etf_codes):
        raise ValueError("Select at least one ETF and list each code once")
    multiple = len(etf_codes) > 1
    if multiple and config.cash_payment_dates_path is not None:
        raise ValueError("Use separate runs for ETF-specific cash payment date files")

    timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d_%H%M%S")
    run_id = f"{timestamp}_{uuid4().hex[:8]}"
    directory = reports_root.resolve() / f"{label}_{run_id}"
    configs = tuple(
        replace(config, etf_codes=(code,), output_dir=directory / code if multiple else directory)
        for code in etf_codes
    )
    directory.mkdir(parents=True, exist_ok=False)
    return run_id, directory, configs
