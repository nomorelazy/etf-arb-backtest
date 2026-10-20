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

## BacktestRunner 回测入口

`etf_arb/runner.py` 的 `BacktestRunner` 是总调度入口；配置和外部输入加载在
`etf_arb/runner_config.py`。Engine 保留独立职责：校验、排序、分发外部
`TradeDecision`，不重新计算信号。当前提供手动决策回放，可以先验证数据、
日期和事件调度，执行与结算算法通过独立接口接入。

从项目根目录运行：

```powershell
python -m etf_arb.cli backtest --config configs/runner.example.json
```

示例读取桌面的两份真实数据，回放 2025-01-02 和 2025-01-03 的两条人工示例
决策；其中的价差和成本仅为示例输入，没有通过行情计算。`mode=replay` 不调用
执行器，不生成成交或收益；报告里的 `net_pnl=null`，不会把估算价差计作利润。
示例已运行的结果可查看 `reports/runner_demo/run_summary.json`。

配置中的路径以配置文件所在目录为基准。`trading_days` 必须提供，既可为递增、
唯一的日期数组，也可为交易日文本文件路径（UTF-8，每行 `YYYY-MM-DD`，
支持空行和 `#` 注释）。不使用“周一至周五”猜测交易日。`starting_cash` 使用
字符串金额，它作为账户模块初始化参数保存；Runner 自身不实现现金余额投影。
`clock_times` 默认使用源数据的 242 个分钟标签，可显式设置其他时点；它同时
决定缺失检查所要求的时间标签，不代表这些时点都允许下单。

运行顺序为：读取并校验外部决策 → 初始化仓库 → 缺失检查 → 建立行情索引 →
逐日/逐时点调度 → Engine 分发决策 → 调用结果报告接口。默认启用质量检查，
有缺失或未检查项时保存明细并停止。`quality_check=false` 可显式跳过检查，
输出会记录这一选择。同一时刻按“日初 → 已排队回调 → 时钟/信号源 → 决策分发
→ 日末”处理；回调中创建的同一时刻事件在当前事件之后执行。

Python 接入接口如下：

| 接口 | 职责 |
| --- | --- |
| `DecisionSource.on_time(context)` | 在当前时点返回已计算的决策，决策时间必须等于 `context.now` |
| `DecisionExecutor.handle(decision, context)` | 创建并执行交易计划；Runner 不代替执行器计算成交 |
| `RunnerHooks.on_day_open/on_time/on_day_close` | 推进外部执行、结算、账户快照等独立组件 |
| `context.schedule(at, callback, label)` | 安排将来的成交/到账/结算回调，回调接收该时点的新 context |
| `context.get_close(code)` | 获取当前日期、不晚于当前时点的最近 close，不跨日回退、不生成买卖盘口 |
| `reporter(result, resources)` | 根据外部账户、成交和结算结果生成最终报告 |

`mode=execute` 必须通过 Python API 注入执行器，并提供历史规则仓库或
`rules_path`，不能仅切换 JSON 开关就声称发生了交易。规则文件接受
`etf_rules`、`security_rules` 数组，字段与现有规则模型对应；缺少历史规则
会明确报错，不使用今日规则回填。没有提供规则的回放模式初始化空规则仓库，
不会自动创造交易规则。

`end_date` 是允许发起决策的截止日期。要继续处理跨日结算，可配置更晚的
`settlement_end_date`，并在 `trading_days` 中列出尾期交易日；尾期仍推进回调，
但不再调用新决策信号源。超出运行日历的定时事件会报错，避免静默丢弃结算。

每次使用新的空输出目录。输出包括 `config.json`、`quality/`、
`market_catalog.sqlite3`、`event_log.csv`、`decisions.csv` 和 `run_summary.json`。
行情索引写入输出目录，原始数据不改写。回调失败会向上传播并写失败摘要，
之前已发生的处理不由 Runner 自动回滚。Runner 实例只允许运行一次。
再次运行示例前，请将配置的 `output_dir` 改成新的目录，以保留上次结果。

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

## 缺失数据检查

`check-missing` 只读取 PCF 和分钟 Parquet，输出缺口明细；不修改原始数据、
不补值、不检查异常价格或处理结算。例：

```powershell
python -m etf_arb.cli check-missing `
  --pcf-root ..\gtfund_pcf `
  --minute-root ..\ifind_minute_close `
  --etf 159338 159516 512760 512880 515880 `
  --start 2025-01-02 --end 2026-07-22 `
  --output reports/missing
```

输出 `summary.json` 和 UTF-8 BOM 的 `missing.csv`。检查内容包括 PCF 文件、
来源声明的成分股总行数、预估现金字段、现金替代标志及必须替代项的现金金额，以及 ETF/需行情成分股
整日或逐分钟的非空 close。相同键重复出现不增加覆盖率；非空的零值或异常
价格仍属于“已存在”，交给独立异常检查处理。无法读取的文件、无法识别的
标志和无法确定市场的证券会列为 `UNCHECKED`，不会静默视为检查通过。
PCF JSON 按源文件表头兼容 6、7、8 列布局，避免把赎回折价比例当成替代金额；
同时支持预估现金字段的 `EstimateCashComponent` 和 `EstimatedCashComponent` 名称。

默认使用现有 iFinD 数据的 242 个时间标签（09:30—11:30、13:00—15:00，
两端包含），包括 11:30 和收盘集合竞价时间；这不是下单可用时段。Python
接口 `check_missing(expected_times=...)` 可传入其他来源的时间标签。
缺行情只代表未发现对应记录，不据此判定停牌，也不会用旧价格填补。

未提供交易日列表时，检查日期取指定区间内“所选 ETF 的 PCF 日期与行情
日期”的并集，报告 `calendar_source=observed`；两边同时缺失的整日不能
由这种参考发现，不能据此声称区间数据完整。可加 `--trading-days days.txt`，
提供真实交易日列表（UTF-8，每行一个 `YYYY-MM-DD`，支持空行和 `#` 注释）。
应按所选 ETF 的存续区间检查，避免把上市前日期当成缺失。仅在提供外部
日期参考、存在检查日期且没有缺口或未检查项时，`passed` 才为 `true`。
发现缺口或未检查项时命令返回码为 1；没有发现问题时为 0，但仍需查看日期参考。

## 尚未实现

1. 将分钟收盘数据仓库接入事件驱动回测，并补充除 close 外的盘口/成交量数据适配器。
2. 目标 ETF 的历史申赎规则、现金替代和交易日结算配置。
3. 结合行情粒度、成交量和涨跌停状态的成交模型。
4. 将信号、订单、申赎状态、资金/持仓账本串起来的事件驱动回测。

在这些部分实现并校验前，框架中的价格估算不能解释为可实现收益或完整回测结果。
