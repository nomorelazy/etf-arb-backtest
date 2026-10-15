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

## 尚未实现

1. 将分钟收盘数据仓库接入事件驱动回测，并补充除 close 外的盘口/成交量数据适配器。
2. 目标 ETF 的历史申赎规则、现金替代和交易日结算配置。
3. 结合行情粒度、成交量和涨跌停状态的成交模型。
4. 将信号、订单、申赎状态、资金/持仓账本串起来的事件驱动回测。

在这些部分实现并校验前，框架中的价格估算不能解释为可实现收益或完整回测结果。
