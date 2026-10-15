# 申赎套利回测系统

ETF 申赎套利回测系统。当前仓库建立了回测系统的 Python 包框架，并实现了核心领域对象、PCF 清单解析与文件仓库、iFinD 分钟收盘价 Parquet 仓库、篮子买卖估值和统一决策关口。历史申赎规则、成交模型和完整事件回测仍需基于目标 ETF 的实际规则实现。

## 环境

- Python 3.10 或更新版本
- 分钟 Parquet 读取需要 PyArrow；生成可视化图表需要 Matplotlib（安装项目时由 `pyproject.toml` 声明）

## 运行

在项目根目录执行：

```powershell
python -m etf_arb.cli --help
python -m etf_arb.cli --version
python -m etf_arb.cli status
python -m unittest discover -s tests -v
```

## 生成可视化报告

回测完成后，将按交易日汇总的账户总权益保存为 `date,equity` CSV；每笔已完整平仓套利交易的净 PnL 保存为 `trade_id,net_pnl` CSV。例如：

```powershell
python -m etf_arb.cli report `
  --equity data/equity.csv `
  --trades data/trades.csv `
  --starting-cash 1000000 `
  --output reports/run_001
```

收益曲线第一条记录必须是回测期起始资金点。输出为 HTML 指标报告、累计 PnL/回撤 PNG 图、净值 CSV 和指标 CSV。指标基于日频权益和已平仓交易，年化默认按 252 个交易日，参数可通过 `--periods-per-year` 调整。最大回撤按正数幅度展示；胜率排除盈亏为零的交易；盈亏比为平均盈利/平均亏损绝对值；收益回撤比采用 Calmar 口径（年化收益/最大回撤）。

## PCF 清单读取

PCF 原始文件继续按 ETF 代码和交易日存放，不会在读取时改写：

```text
gtfund_pcf/
├─ 159338/20241015.xml
└─ 512760/20190612.json
```

解析器支持深交所 XML、上交所 XML 和旧版上交所 JSON。回测代码通过文件仓库按 ETF 代码与生效日期读取；找不到清单或清单中的代码、交易日不匹配时会明确报错：

```python
from datetime import date
from etf_arb.data import FilePCFRepository

pcf_repo = FilePCFRepository(r"C:\data\gtfund_pcf")
pcf = pcf_repo.get("159338", date(2024, 10, 15))
print(pcf.creation_unit, len(pcf.components))
```

PCF 的 `<TradingDay>20241015</TradingDay>` 解析为交易日 `date(2024, 10, 15)`，`<PreTradingDay>20241014</PreTradingDay>` 解析为前一交易日 `date(2024, 10, 14)`；均要求严格的 `YYYYMMDD` 格式，不以自然日减一推断前一交易日。

数量、价格、比例和现金字段都以 `Decimal` 保留。现金占位行会保留用于核对，但不应作为股票行情读取；上交所 XML 的成分股代码取 `InstrumentID`，`UnderlyingSecurityID` 保留作市场代码。不同来源的估算、实际和前日现金字段各自保留，解析阶段不会将它们猜测成统一的申购/赎回现金调整。原始清单是审计源，解析对象在内存中使用。

## 分钟收盘行情读取与目录索引

iFinD 分钟收盘价按月存成 Parquet，行情数据使用长表结构：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `time` | 无时区 timestamp | 分钟时间戳，按上海本地时间解释 |
| `thscode` | 字符串 | iFinD 证券代码，包含交易所后缀，例如 `000001.SZ`、`512760.SH` |
| `close` | 浮点数 | 分钟收盘价 |

扫描器只读取形如 `YYYY-MM/*.parquet` 的文件；同目录中的报告、脚本和其他文件会被忽略。它把源文件路径、大小、更新时间、字段类型、行数、时间范围，以及逐文件的证券代码和行数写入项目的 `data/market/` SQLite 目录索引。SQLite 不复制行情行；Parquet 仍是数据源。不同数据根目录会生成独立索引。修改过的文件会在下次扫描时重建索引，已删除文件会从索引清理。

```powershell
python -m etf_arb.cli scan-minute-close --root ..\ifind_minute_close
```

若需强制重新检查所有文件，可加 `--force`；也可以用 `--catalog` 指定 SQLite 索引的位置。代码中可直接创建仓库查询：

```python
from datetime import datetime
from etf_arb.data import ParquetMinuteCloseRepository

minute_data = ParquetMinuteCloseRepository(r"C:\data\ifind_minute_close")
bars = minute_data.get_closes(
    "159338.SZ",
    start=datetime(2025, 1, 2, 9, 30),
    end=datetime(2025, 1, 2, 10, 0),
)
```

行情字段 `time` 为 Parquet `timestamp[us]`（无时区），数值本身即为北京时间；读取和查询不进行 UTC / 上海时区转换。查询参数 `start`、`end`、`get_at_or_before` 的时间同样要求传入无时区的北京时间。

如果只需要参与 A 股连续竞价的分钟行情，可在读取时指定：

```python
from datetime import date, datetime
from etf_arb.data import ParquetMinuteCloseRepository
from etf_arb.rules import is_continuous_auction_time

repo = ParquetMinuteCloseRepository(r"C:\\data\\ifind_minute_close")
bars = repo.get_closes(
    "000001.SZ",
    start=datetime(2024, 10, 15, 9, 30),
    end=datetime(2024, 10, 15, 14, 57),
    continuous_auction_only=True,
)
# 仅保留 09:30 <= time < 11:30 或 13:00 <= time < 14:57
# 午休、开盘前、14:57 起的收盘集合竞价均不参与撮合。
```

`get_closes()` 默认仍返回全部原始分钟行情，用于审计、数据检查；传入 `continuous_auction_only=True` 才过滤时段。订单撮合接口也会拒绝在连续竞价时间外下单，且不会跨午休使用上午旧报价。

查询以 `(time, thscode)` 为键。重叠文件中相同键、相同 close 会折叠为一条并保留来源文件；close 不同会抛出冲突错误，不会静默挑选。源时间无时区，按上海本地时间处理；行情价格读取为 `Decimal`。扫描时会检查必要列、类型、空值、非正数和非有限 close。SQLite 索引文件已加入 `.gitignore`。

也可以安装为本地命令：

```powershell
python -m pip install -e .
etf-arb --help
```

## 目录

```text
etf_arb/
├─ data/          PCF、行情和数据校验
├─ rules/         带生效日期的申赎规则
├─ pricing/       篮子估值与方向性毛价差
├─ decision/      环境检查和套利条件的统一关口
├─ execution/     订单、成交结果和撮合接口
├─ portfolio/     按时间记录账户事件
├─ engine/        回测编排接口
├─ report/        回测结果结构
└─ cli/           命令行入口
```

金额和价格使用 `Decimal`。导入数据时请从原始字符串构造 `Decimal`，避免先经过二进制浮点数。

## A 股 ST、退市整理期和停牌规则

在 `etf_arb/rules/a_share.py` 中，A 股每日规则使用三个明确的布尔标志：

- `is_st=True`：ST 或 *ST 风险警示股票（两者共用标志；应使用该交易日真实证券状态）。
- `is_delisting_period=True`：处于退市整理期（不是“已摘牌”）。
- `is_suspended=True`：股票当日全天停牌，不得买卖。

退市整理期第 1 个**实际可交易日**无涨跌幅限制；第 2—15 个实际可交易日恢复对应板块涨跌幅：主板 ±10%、创业板/科创板 ±20%、北交所 ±30%。退市整理规则优先于 ST 的历史涨跌幅限制。第 15 个实际可交易日之后不允许按退市整理期继续交易。停牌日不计入 15 天；现行交易所规则规定，退市整理期内全天停牌累计不得超过 5 个交易日。

使用 **交易所交易日序列** 计算序号，不使用自然日差值，也不根据分钟行情缺失推断停牌：

```python
from datetime import date
from etf_arb.rules import (
    AShareBoard, delisting_trading_day_index, make_a_share_security_rule,
)

# 示例日历必须从退市整理期首个交易所交易日开始连续给出，
# 包括当中已知的全天停牌日，不包括休市日。
exchange_days = [
    date(2026, 10, 15),
    date(2026, 10, 16),
    date(2026, 10, 19),
    date(2026, 10, 20),
]
suspended_days = [date(2026, 10, 16)]
day = date(2026, 10, 19)
ordinal = delisting_trading_day_index(
    day, exchange_days, suspended_days=suspended_days
)  # 2，10 月 16 日停牌未计数
rule = make_a_share_security_rule(
    "600001.SH", day, AShareBoard.MAIN,
    is_st=True, is_delisting_period=True,
    delisting_trading_day=ordinal, is_suspended=False,
)
assert rule.limit_up_rate == rule.limit_down_rate
assert rule.can_buy() and rule.can_sell()
```

若查询的日期本身全天停牌，`delisting_trading_day_index` 返回 `None`，建立规则时传 `is_suspended=True, is_delisting_period=True`，并**不传交易日序号**，买卖都会禁用。当天复牌后继续递增之前的有效交易日序号。序号为 16 或以上时，规则自动禁用买卖。普通主板 ST 的历史规则仍为 2026-07-06 之前 ±5%、当日起 ±10%。

对实际订单执行 `NextQuoteFillModel(security_rules=InMemoryRuleRepository(...))`，撮合会读取当日股票规则，拦截停牌、已结束退市整理期以及被禁止的买卖方向。未注入规则仓库的旧式撮合仅能依赖行情快照中的停牌标志，不能根据证券主数据识别当天停牌；完整回测应注入规则仓库。

## 尚未实现

1. 将分钟收盘数据仓库接入事件驱动回测，并补充除 close 外的盘口/成交量数据适配器。
2. 目标 ETF 的历史申赎规则、现金替代和交易日结算配置。
3. 结合行情粒度、成交量和涨跌停状态的成交模型。
4. 将信号、订单、申赎状态、资金/持仓账本串起来的事件驱动回测。

在这些部分实现并校验前，框架中的价格估算不能解释为可实现收益或完整回测结果。
