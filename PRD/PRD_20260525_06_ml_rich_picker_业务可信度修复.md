# PRD_20260525_06 ml_rich_picker 业务可信度修复

## 1. 元信息

| 字段 | 内容 |
|---|---|
| LLM型号 | GPT-5 Codex |
| 输出时间戳 | 2026-05-25 16:30:00 |
| 文档编号 | PRD_20260525_06 |
| 关联 Commit | 未提交 |
| 需求优先级 | P0 |

## 2. 问题描述

`ml_rich_picker` 已具备富特征训练和回测能力，但仍有几类会影响业务可信度的问题：

1. 回测撮合/NAV 默认读取 qfq 行情，成交价、资金、费用和持仓成本不是现实交易口径。
2. 本地 K 线缓存不区分 `qfq/none/hfq`，可能串用不同复权口径。
3. rich sell 持仓状态特征在执行价改为 none 后不能继续把 qfq close 除以 none 成本价。
4. 交易固定使用 `decision_horizon=5`，但 rich 模型包仍强制 h1/h10/h20 齐全。
5. 股票池不能只看当前 active 股票，否则历史回测存在幸存者偏差。
6. sell 标签只覆盖未来风险回撤，不能完整表达“继续持仓是否值得”。

## 3. 影响模块

| 模块 | 是否影响 | 说明 |
|---|---|---|
| `engine` | 是 | BacktestEngine 增加 `bar_adjust`，默认 `none`。 |
| `data_layer` | 是 | BigQuery 缓存 key 加复权口径；股票池支持 as-of 过滤。 |
| `strategy/ml_rich_picker` | 是 | rich 推理/训练按 h5 决策、sell 状态和标签重构。 |
| `config` | 是 | 增加成交行情复权口径配置。 |
| `tests` | 是 | 增加成交口径、缓存、as-of 股票池、rich 持仓状态测试。 |

## 4. 需求详情

### 成交口径

回测撮合、资金、费用、NAV 使用真实不复权价格 `none`。策略训练/打分特征仍可使用 `qfq`，但必须在策略层显式读取，不能让回测引擎默认 qfq。

### 缓存口径

本地 K 线缓存 key 必须包含完整证券代码和 `adjust_type`，避免 `000001.SZ qfq` 与 `000001.SZ none` 互相污染。

### rich sell 持仓状态

sell 模型的持仓状态特征继续使用训练口径的 qfq 价格：实际成交日 qfq open、当前 qfq close、持仓期 qfq high。执行止损、追踪止盈和成交仍使用 none 价格。

### 模型包依赖

正式交易只强制 `buy_h5 + sell_v1`。`buy_h1/buy_h10/buy_h20` 可继续训练保存作诊断，但缺失时不得阻塞 `decision_horizon=5` 的回测。

### 股票池 as-of

动态股票池按信号日判断 `list_date <= as_of` 且 `delist_date > as_of`，rich 预加载使用全历史股票池后再按当日特征截面收敛。

### sell 标签

sell 标签应覆盖两类“应卖”情形：

1. 继续持有后未来窗口出现不可接受回撤。
2. 继续持有后未来收益落在同日截面底部，用于表达机会成本和 alpha 衰减。

## 5. 不做范围

严格 PIT qfq 需要补采 Tushare 快照或自行按 `daily none + adj_factor` 构造 as-of 复权序列。严格 ST PIT 过滤需要历史 ST/风险警示状态表。本 PRD 只修复当前代码不应把 qfq 当真实成交、不应混用 qfq/none，以及可由现有表直接表达的 as-of 上市/退市股票池逻辑。

## 6. 验收标准

1. BacktestEngine 默认 `adjust=none` 预加载交易行情。
2. BigQueryDataSource 单标的缓存 key 区分 `qfq/none/hfq`。
3. `get_stock_list(as_of_date=...)` 和 `get_liquidity_top_equities(...)` SQL 包含 list/delist 日期条件。
4. rich 策略缺少非 h5 buy 模型不失败，缺少 h5 仍失败。
5. rich sell 持仓状态收益不使用 none 成本价除 qfq close。
6. 相关 pytest 全部通过。
