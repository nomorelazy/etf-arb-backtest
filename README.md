# 申赎套利回测系统

ETF 申赎套利回测系统。支持 159338、159516、512760、512880、515880 的分钟信号、申赎执行、现金替代模拟、账户和逐笔盈亏报告。同一条命令可依次回测多只 ETF，每只独立记账。

后续修改以[本轮执行基线](docs/requirements_baseline.md)及用户最新指令核对，不能将试跑参数或研究假设改写为长期要求。

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
`TradeDecision`，不重新计算信号。支持手动决策回放，以及下文的完整研究模拟。

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

`mode=execute` 需要通过 Python API 注入执行器，或配置 `simulation` 使用内置研究模型，
同时提供规则仓库或 `rules_path`。规则文件接受
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

## 完整研究回测

修改 `configs/full_flow.example.json` 中的 PCF 和分钟数据路径，然后运行：

```powershell
python -m etf_arb.cli backtest --config configs/full_flow.example.json --premium-threshold 0.005
```

该模式通过 `etf_arb/full_flow.py` 组合已有 Runner 及以下模块：

| 模块 | 职责 |
| --- | --- |
| `data/snapshot.py` | 按日批量读取和缓存分钟数据，拒绝冲突重复价，查询不使用未来分钟 |
| `pricing/premium_discount.py` | 独立计算每份折溢价；比例和百分数接口分别保留 |
| `decision/premium.py` | 按本轮配置的阈值生成申购/赎回决策，等于阈值不触发 |
| `execution/plan.py` | 按基金上市市场和 PCF 区分本市场实物、固定现金和跨市场代理；深市 ETF 的 159900 汇总现金只计一次 |
| `execution/simulator.py` | 后续分钟成交、申赎确认、未成交处理、跨日退出和结算推进 |
| `settlement/cash.py` | 按交易日处理现金替代款项和 PCF 现金差额 |
| `portfolio/account.py` | 现金与持仓投影，禁止透支及卖空；按实际模拟现金流计算交易最终盈亏 |

在配置的 `etf_codes` 中保留一个基金代码。多只一起跑时，在规则文件中配置各基金的有效期，并在一条命令中列出要运行的基金。

```powershell
python -m etf_arb.cli backtest --config configs/full_flow.example.json --etf 159516 512760 512880 515880 --premium-threshold 0.002
```

使用 `--etf` 时，每次命令在 `reports` 下新建一个回测目录。多只 ETF 的报告在该目录内按代码分开，例如 `reports/backtest_<运行编号>/159516/report/report.html`。单只 ETF 的报告直接放在本次回测目录的 `report` 下。需要提供实际付款时间文件时按 ETF 分别运行，以便逐笔匹配。

159516 与 159338 按深市实物模式处理，三只沪市基金按沪市实物、深市跨市场现金退补模式处理。沪市跨市场申购保证金采用 PCF 参考金额乘以（1＋申购替代溢价比例）。

如需申请时交收带符号的预估现金 E，设置 `simulation.settle_estimated_cash_at_application=true`。下一交易日 PCF 的 `PreTradingDay` 必须对应申请日，深市读取 `CashComponent`，沪市读取 `PreCashComponent`（旧 JSON 对应前一日的 `CashComponent`）。申购差额为 E−A，赎回为 A−E。配置 `cash_difference_lag=0` 可模拟披露当日到账。现金替代退补仍按各自定价和到账配置推进。

用户当前选择直接使用 close 简化成交，交易税费采用用户配置，额外滑点为零，不要求拆出另一套盘口正式运行。
阈值由每次配置或命令行指定，不固定为 0.3% 或 0.4%。上面命令的 0.005 仅演示 0.5%；
请替换为当次希望的比例值，或在 JSON 设置 `simulation.premium_threshold`。命令行值覆盖 JSON，
两处均为空时明确报错，不自动选择旧门槛、不解释成零阈值。源信号继续保留可配置门槛的双向判断。
示例每次 1 个申赎单位，取消每日次数上限（`max_trades_per_day=null`），改为滚动一小时最多发起 1 笔
（`min_trade_interval_minutes=60`）。申购与赎回共用次数；单笔套利内部的股票订单不单独计数。
间隔从上一笔实际提交申购或赎回的 `applied_at` 起算，满 60 分钟后遇到合格信号即可再发起。
例如 09:35 发出信号、09:36 提交申赎，下一笔信号最早为 10:36；不按整点重置，午休也计入实际经过时间。
取消等待上一笔结清的约束，允许多笔套利并行；各笔持仓、费用和现金替代款分别记账、独立结算。
新交易仍需足额可用现金，待到账款项不提前释放。尚未提交申赎的入场先等待实际提交，再启动间隔。
`trades.csv` 和逐笔 HTML 展示 `applied_at`、`next_signal_not_before`；六列 Portfolio 仍按原发起日期归属盈亏。
用户交易时段统一配置为 `trading_sessions=[["09:35","11:27"],["13:00","14:57"]]`，
端点整分钟包含在内。独立信号、外部决策、申赎处理和成交均受同一时段规则约束；
默认还显式配置 `blocked_trade_minutes=["09:30"]`。时段之外的待处理事项顺延至允许的分钟，
不会通过缩短原始数据检查时钟来绕过限制。此前允许 09:30 发起的验证结果不满足当前规则。
当前 close 研究默认按信号 t 分钟的精确 close 生成信号并模拟入场买入；t+1 分钟提交申赎，取得证券后在该申请分钟卖出正常可交易的赎回股票或申购 ETF。
11:27 的待处理订单最早在 13:00 继续；14:57 可以成交，但因当天没有更晚允许分钟，
该分钟不发起新套利。14:56 发起的套利在该分钟买入，可在 14:57 提交申赎并完成正常卖出；仍未成交的真实持仓继续保留。
使用稀疏 `clock_times` 时，同样要求当天配置中还存在更晚的允许成交时钟，才发起新套利。
样例申赎截止同步设置为 14:57；策略时段与交易所连续竞价辅助函数分别管理。

申购买入深市组合证券，足额后提交当日 PCF，支付沪市替代保证金、深市固定替代金额及
正的预估现金部分，取得 ETF 后在 t+1 申请分钟卖出。赎回先在 t 分钟买 ETF，t+1 提交赎回，取得深市股票后
在同一申请分钟卖出，沪市部分进入现金替代结算。预估现金为正时，两方向均预先提供该资金；
结算时退回预付并按最终现金差额补退。预估为负不提前增加可用现金。

申购信号新增组篮检查：当日 PCF 中有数量的真实成分股，如果既无法买入又禁止现金替代，
即使折溢价超过阈值也不发出申购信号；`signals.csv` 记录股票及原因。零数量与申赎现金虚拟行不拦截。
能买入的禁止替代股票仍可正常组篮，赎回卖出检查不因本条件扩大。
深市允许替代股票无法买入时，按本轮用户明确选择的实验公式
`当前分钟精确 close × 替代数量 × (1 + PCF 申购现金替代溢价比例)` 支付保证金并进入现金替代流程，
不把 PCF 的零固定替代金额当作零成本。缺 close 或替代溢价比例时不发起。
这项价格选择是研究假设，不是说明书要求的除权调整后 T-1 日参考价；
保证金和最终成本/退补款分开，代买及日终定价沿用 T+2 14:30/15:00 的已确认约定。

2026-10-27 用户确认沪深时序分离，默认 `market_split_execution=true`：
沪市最新规则：自**实际提交申赎**起至 **T+2 日 15:00（含）**，逐分钟寻找首个可成交时点，申购代买、赎回代卖均按该分钟精确 close 足额定价；若申请分钟可成交，仍使用原 t+1 价格。
深市申购中无法买入、已转现金替代的股票仍统一等到 **T+2 日 14:30** 补券，即使此前已恢复可买也不提前。
固定替代直接读取 PCF，不生成买卖订单。该模式要求 `assume_full_basket_fill=true`，
缺下一允许 Runner 分钟不发起，午休或隔夜不能算作 t+1 分钟；
显式设置 `market_split_execution=false` 可复现旧版所有代买卖统一 T+2 的实验。
申购退补款为汇总保证金减模拟购入金额，赎回收入为模拟卖出金额。
普通研究情形下，`cash_substitution_pricing_days=2` 将定价期限与付款分开：
`cash_substitution_attempt_time="14:30"` 指定深市补券尝试时点（旧模式作用于全部代买卖）。
深市补券只在 T+2 日 14:30 尝试。沪市缺精确 close 或规则禁止时，继续逐分钟检查至 T+2 收盘；
独立研究分钟窗口为 09:30—11:30、13:00—15:00（含端点），不受投资者发起窗口或稀疏 Runner 时钟限制，排除午休与隔夜。
T+2 日 15:00 先检查是否能成交，再将整个窗口均未成交的数量按该日精确收盘价估值。
已成交数量保留首个可成交分钟的价格和税费，不按后续行情重算；估值不产生假成交及成交税费。
该分钟的定时回调和常规时钟不会重复执行；有数量上限时，已成交部分用其成交金额，
剩余数量在 T+2 日 15:00 使用该日精确 close 估值，单独输出到 `substitution_pricing.csv`，
不伪造成投资者成交。缺少该日 close 时保留未定价状态，禁止继续用 T+3 的未来价格填补。
14:30 尝试时点属于本轮研究执行约定，不是说明书规定的强制交易时点。
个股正常交易日延长、20 日特例和期间公司行为需要额外历史输入，当前不能据此宣称已完整模拟这些情形。

说明书普通情形下，T+2 后首个工作日内发送退补明细，款项在此后 3 个工作日内交收；
通知期限、最迟交收期限都不等于实际到账。配置 `settlement_business_days` 提供明确工作日历，
用于计算 `notice_due_on` 与 `latest_payment_on`；未提供时不把交易所日历冒充结算工作日历。
默认 `cash_substitution_lag=null`、`cash_difference_lag=null`，资金只有在已提供的付款时点才入账。
旧式整数 lag 仍可显式启用研究情景，报告会标记 `explicit_research_lag`，不会冒充说明书交收保证。

`cash_payment_dates_path` 可指定 JSON 文件，其每条记录为
`{"trade_id":"T000001","kind":"cash_substitution","paid_at":"2025-01-07T09:00:00"}`。
`kind` 也可为 `cash_difference`。日期只是格式示例，须按实际记录填写；付款事件在其真实时间推进，
不受套利发起窗口限制。时点在当前运行日历以外、款项金额未确定或资金不足时，不标成已收付。
默认没有付款记录，因此定价完成的款项仍会待交收，不会自动释放资金或开启下一笔。

现金差额仅在后续 PCF 的 `PreTradingDay` 等于原申请日时读取其 `CashComponent`；
不会把申请日清单里的历史现金差额用于本次交易。金额确认和实际到账分别保存，
缺少最终差额、付款记录或尾期不够时保持待结算，`final_net_pnl=null`。

当前按用户 2026-10-21 最新选择，默认 `assume_full_basket_fill=true`：假设全部订单可以组齐，
默认 `entry_at_signal_time=true`：赎回买入 ETF、申购买入实物股票改用**信号 t 分钟的精确 close**。先校验完整买入篮子、税费、申请款资金及下一申请分钟许可，再在信号分发时足额成交；申赎申请仍在下一允许分钟（market-split 模式为 t+1）提交，替代保证金和正预估现金仍在申请时扣账。明确设置 `entry_at_signal_time=false` 可复现此前 t+1 买入路径；部分成交实验须关闭此项。
投资者委托不受每分钟数量上限影响，也不因现金只够部分数量而缩单。整篮所需行情或足额资金缺失时，
该轮提示错误，不生成部分持仓与 `HELD_PENDING_POLICY`，不把缺失数据伪造成成交。
旧式未组齐暂停处置仅在显式设置 `assume_full_basket_fill=false` 且 `market_split_execution=false` 的独立部分成交实验中保留。
已申赎成功但没有卖出的 ETF/股票继续持有，等待后续有效分钟及交易规则。
`max_shares_per_minute` 限制深市 T+2 定时补券（旧模式则限制全部定时代理）；沪市在首个可成交分钟足额执行。只有关闭整篮假设时，它才同时限制投资者委托。
缺少成交量和盘口，不能据此判断真实成交能力。

2026-11-04 最新时序：赎回 ETF 在 t 分钟买入，t+1 提交赎回并卖出正常可交易的沪深股票；申购实物股票在 t 分钟买入，t+1 提交申购并取得 ETF 后，在同一分钟按精确 close 卖出 ETF。缺少精确卖价或规则禁止卖出时保留 ETF 持仓，后续仅记录真实允许分钟的模拟成交，不用旧价伪造 t+1 成交。沪市代买卖窗口及深市 T+2 14:30 补券独立保留。信号价与实际成交价的归因照常计算，不将任何盈亏项硬置零；正常赎回 ETF 买入价与信号价相同时，该项偏差自然为零。显式 `entry_at_signal_time=false` 保留原 t+1 买入及申请后更晚卖出 ETF 的路径。
缺精确卖价或规则禁卖时，不伪造立即成交；深市实物股票继续持有并在后续允许分钟重试，沪市代理在申请至 T+2 收盘的窗口继续逐分钟寻找首个可成交时点。
逐笔输出 `redemption_prices_locked_at` 和 `delayed_redemption_components`，报告区分已完全锁价与含延迟卖出的赎回。
沪市按首个可成交分钟定价后，最终现金替代金额仍在原定价期限确认，实际付款依旧独立。
`orders.csv` 分别记录 `next_minute_attempted_at` 和 `manager_attempted_at` 及沪市 `last_sh_attempted_at`，
`substitution_attempts.csv` 的 `timing` 区分沪市申请当分钟、沪市窗口内后续逐分钟尝试与深市定时补券；标准 HTML 明示时序。

`security_rule_policy="require"` 是默认严格模式，缺少当日证券规则的委托不成交。
样例显式使用 `"assume_tradable"`：没有历史状态资料时假设有效分钟可交易；提供的停牌、
买卖开关和时段规则仍然生效，已有规则序列的日期缺口不会被忽略。
这份数据未提供历史停牌、除权参考价和涨跌停盘口，不能据此声称已模拟这些限制。
`configs/research_rules.example.json` 是研究规则样例，实际每日 PCF 的申赎开关及额度还会再次校验。
不支持的物理申赎模式、成份数量或标志会在 `rejections.csv` 明确记录。

每次输出包含信号、订单、成交、现金流水、结算明细、交易周期、逐日账户和 HTML 报告：
`signals.csv`、`orders.csv`、`fills.csv`、`cashflows.csv`、`cash_settlements.csv`、`trades.csv`、
`daily_account.csv`、`financial_summary.json`、`assumptions.json`、`portfolio_ledger.csv`、
`substitution_pricing.csv`、`substitution_attempts.csv`、`creation_substitutions.csv`、`report/report.html` 和 `report/ledger.html`。
税费另存 `fees.csv`；`fills.csv` 保留佣金、印花税、过户费、经手费和费用合计，`financial_summary.json` 与标准 HTML 显示总费用。
Portfolio 保留旧 `TradeRecord`、`record_trade`、`record_settled_trade`、`to_rows`、`export_csv` 接口，
同时支持交易生命周期和期间汇总。六列交易额度采用 ETF 腿实际成交额：申购为卖出 ETF 收入，
赎回为买入 ETF 支出；交易单位输出 ETF 份数，不以 `quantity_units` 倍数代替。
正式 report 模块提供完整逐笔现金/证券事件、代买卖依据及定价/通知/付款状态，支持展开打印全部，
不再依赖 Temp 中的临时导出脚本。尚未结清的现金流不标成最终盈利或亏损。
`report/report.html` 直接使用标准 `report.render.save_report` 模板生成，不在完整流程中追加或重写页面。
复制到其他目录查看时，应将它与同目录的 `pnl_report.png` 一起复制。

交易税费统一在 `etf_arb/costs/fees.py` 计算。当前默认按用户研究设置：股票佣金万1、过户费万1，买卖双向；股票卖出另收印花税万5。ETF佣金万0.5、经手费万0.4，买卖双向叠加。每笔成交、每个费用项目四舍五入到分，无最低佣金。对应100万元成交额，股票买入200元、卖出700元，ETF买入或卖出各90元。

费率通过 `simulation.fees` 配置，样例见 `configs/full_flow.example.json`；五项费率显式设为零可复现此前零费实验，代码中可使用 `FeeRates.zero()`。此配置是完整模拟器唯一收费来源，不会再按证券规则中的费用元数据重复收费。
投资者买入扣成交额及税费、卖出入账成交额减税费，账户分别记录四类费用；整篮预检先保证包括买入税费在内的足额资金。基金经理实际模拟成交的费用加入代买成本或扣减代卖收入，通过退补款体现，投资者现金不重复扣费。未成交部分的收盘估值、申赎划转和固定PCF现金替代金额不额外模拟证券交易税费。
信号用当时可见价格估算费用并输出 `estimated_fee`、`estimated_net_edge`；原始 IOPV、折溢价及触发门槛保持独立，Engine仍只校验和分发。最终净盈亏由含费用现金流汇总；`save_report(total_costs=...)` 仅报告成本，不再次扣减盈亏。
其中 `final_net_pnl` 回答“这段时间发起的套利最终共赚多少”；仍有未完成交易时返回空值，
同时保留已完成部分 `settled_net_pnl`。`ending_equity` 包含未出售持仓和未结算应收应付款的估值，
不等同于全部已实现盈亏。每天的陈旧持仓估值会在 `stale_marks` 列标记。

实现参考[159338 招募说明书（2024 年第一号）](https://st.gtfund.com/report/2024/10/国泰中证A500交易型开放式指数证券投资基金更新招募说明书（2024年第一号）.pdf)
第 40–51 页的 IOPV、跨市场现金替代及现金差额说明；模拟确认时点、代买卖 close 和显式 lag 为研究假设。
运行日历必须明确提供。若从数据中观测日期生成，仍需用独立交易所日历检查两份数据同时缺失的交易日。

## 生成可视化报告

回测完成后，将按交易日汇总的账户总权益保存为 `date,equity` CSV；每笔已完整平仓套利交易的净 PnL 保存为 `trade_id,net_pnl` CSV。例如：

```powershell
python -m etf_arb.cli report `
  --equity data/equity.csv `
  --trades data/trades.csv `
  --starting-cash 1000000 `
  --output reports/run_001
```

收益曲线第一条记录必须是回测期起始资金点。输出为 HTML 指标报告、累计 PnL/回撤 PNG 图、净值 CSV 和指标 CSV。指标基于日频权益和已平仓交易，年化默认按 252 个交易日。夏普比率以 1.15% 为默认年化无风险利率，按复利换算为日利率后从日收益扣除。单独生成报告时可用 `--periods-per-year` 和 `--risk-free-rate` 覆盖。最大回撤按正数幅度展示，胜率排除盈亏为零的交易，盈亏比为平均盈利除以平均亏损绝对值，收益回撤比为年化收益除以最大回撤。

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

## 逐笔价差与现金盈亏报告

报告统计本轮申购、赎回各自发起笔数，并直接显示申购总盈亏、赎回总盈亏：分别按全部交易的含费最终净盈亏汇总，不随明细筛选变化；有待结清交易时注明未确定笔数，没有该方向交易时显示 0.00。逐笔归因表按页面宽度完整展开，所有记录随页面显示，金额列和未成交估值数量在表尾合计，筛选后合计同步更新。折溢价率不相加，未知金额仅显示已知合计及缺失笔数。HTML 取消重复的六列账本展示，六列 Portfolio 接口及 `portfolio_ledger.csv` 继续保留。

完整回测自动在 `report/report.html` 增加逐笔归因表，并在运行目录及 `report/` 内导出 `trade_attribution.csv`。每笔展示发起时间、方向、最终净盈亏、信号折溢价率、毛价差、现金替代价格盈亏、预估现金 E、实际现金 A 和现金盈亏影响；可筛选亏损及方向，点击交易编号进入完整账目。归因表、摘要及归因 CSV 不展示交收时间、状态、信号 ETF 价格、IOPV 或每份价差；原始执行流水与内部对账基准继续保留。原六列 Portfolio API 保持不变。

现金替代价格盈亏相对信号估值计算，已包含代理税费，保证金退还本金不算盈利；退补款／应收款另列。预估现金影响为申购 `E-A`、赎回 `A-E`，与原执行模块确认的实际现金对账。已结清交易满足：毛价差＋ETF 价格影响＋实物股票价格影响＋含代理费的替代盈亏＋预估现金影响−投资者税费＝净盈亏。未定价金额和未结清最终盈亏留空，定价与实际到账分别记录。

旧回测无需重跑，可读取既有账目生成新版正式报告。`--output` 使用原运行目录下全新的子目录，保留旧报告和原始 CSV：

```cmd
python -m etf_arb.cli report-attribution --run-dir "reports\原运行目录" --output "reports\原运行目录\report_detailed"
```

旧输出缺少信号估值快照时，命令还需要原配置指向的 PCF 和分钟数据，仅查询发起分钟及前一分钟并检查逐笔盈亏对账；新回测直接保存信号快照。此命令不启动回测或改变成交、结算金额。

## 尚待补充的历史数据与模型

1. 实际基金经理交易、退补款到账时间与结算工作日历。
2. 历史停牌、涨跌停、公司行为及个股正常交易日延长/20 日特例所需输入。
3. 除 close 外的盘口/成交量适配器；当前实验按用户选择直接使用 close，不因此虚构盘口。
4. 非交易日资金事件的日历调度。

现有研究流程可以运行，但缺失的交收输入会保留待结状态；模拟成交与估值不能解释为真实可实现收益。
