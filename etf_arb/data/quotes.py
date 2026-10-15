"""行情数据源接口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from etf_arb.domain import Quote


class QuoteRepository(Protocol):
    def get(self, security_code: str, ts: datetime) -> Quote | None:
        """返回不晚于指定时点且符合数据策略的行情；不得读取未来行情。"""
        ...
