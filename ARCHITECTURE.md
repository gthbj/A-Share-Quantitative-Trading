# A-Share Quantitative Trading — 架构文档

> **读者对象**：开发者、维护者、AI 助手（LLM）  
> **目标**：快速理解本项目的整体架构、各模块职责、模块间依赖关系以及关键设计决策。

---

## 1. 架构概览

本项目是一个 **纯模拟** 的 A 股量化回测与虚拟盘框架。核心设计遵循 **分层解耦** 原则，从上到下依次为：

```
┌────────────────────────────────────────────┐
│  CLI / Jupyter (用户入口)                  │
│  run_backtest.py / notebooks/demo.ipynb    │
├────────────────────────────────────────────┤
│  Strategy Layer (策略层)                   │
│  BaseStrategy → DoubleMA / Momentum / ...  │
├────────────────────────────────────────────┤
│  Engine Layer (引擎层)                     │
│  BacktestEngine / PaperTrader              │
│  TradeEngine (撮合 + 费用 + A股规则)       │
├────────────────────────────────────────────┤
│  Account Layer (账户层)                    │
│  Portfolio (资金) + Position (持仓/T+1)    │
├────────────────────────────────────────────┤
│  Data Layer (数据层)                       │
│  BaseDataSource → BigQueryDataSource       │
│  LocalStorage (Parquet/CSV 缓存)           │
├────────────────────────────────────────────┤
│  Analytics & Utils (绩效与工具)            │
│  Metrics / Plotter / Report / Calendar     │
└────────────────────────────────────────────┘
```

**数据流向**：
1. `Data Layer` 提供历史行情 → 
2. `BacktestEngine` 按交易日历逐日调度 → 
3. `Strategy` 在 `Context` 中生成 `Order` → 
4. `TradeEngine` 根据 A 股规则撮合 → 
5. `Portfolio/Position` 更新资金与持仓 → 
6. `Analytics` 计算绩效并输出图表/报告。

---

## 2. 模块职责

### 2.1 data_layer/ — 数据层

| 文件 | 职责 |
|------|------|
| `base_data_source.py` | 定义 `BaseDataSource` 抽象基类与 `Bar` 数据模型。统一接口 `get_bars(code, start, end, period)` 支持 `"daily"`、`"1min"`、`"5min"`、`"15min"`、`"30min"`、`"60min"` 多周期行情获取。 |
| `bigquery_source.py` | **当前默认数据源实现**。通过 google-cloud-bigquery 连接 Google Cloud BigQuery（项目 `data-aquarium`，dataset `ashare`，asia-east2）拉取 A 股历史行情。包含本地 Parquet 缓存与缓存清理策略（按保留天数 + 总容量上限）。当前依赖 `ashare.dwd_*` 标准字段表；ODS external table、DWD native table、DWS 特征层与 ADS 信号层均已生成并通过 audit，财务指标 DWD、估值特征和基本面特征已完成专项修复/生成，BigQuery ML baseline 已生成 ADS 候选信号。另提供可选 DWS 股票特征快照/训练集查询 helper、BQML ADS 候选信号读取 helper、日线股票批量行情预加载能力，以及按回测首日前历史成交额选取流动性 Top-N 股票池的 helper，供 ML / BQML 策略使用。数据源会读取 `account.trading_permissions`，默认过滤当前账户无专项权限的北交所、科创板、创业板、ST/退市整理等标的，不改变 `BaseDataSource` 抽象接口。详见 §4.6 / §4.7 / §4.8 / §4.16。 |
| `maxcompute_source.py` | 阿里云 MaxCompute 数据源实现（保留为备选）。通过 pyodps 连接。历史支持：5min K 线、15min ETF K 线。 |
| `akshare_source.py` | AKShare 免费数据源实现（已保留为备选，但未被 `run_backtest.py` 装载）。首次请求调用 API 拉取并写入 `LocalStorage`；后续优先读本地缓存，支持增量更新。 |
| `local_storage.py` | 本地数据缓存管理器。支持 Parquet/CSV 格式，按 `data/raw/daily/{code}_{period}.parquet` 组织（如 `000001_1min.parquet`），避免不同周期数据互相覆盖，提供按日期范围快速索引；读取缓存时统一将 `open/high/low/close/volume/amount` 转为数值，避免 BigQuery Decimal 缓存命中后进入撮合计算。 |

**设计要点**：
- 抽象接口隔离具体数据源，便于后续接入 Wind、Tushare Pro 等付费源。
- 缓存策略为 **写时缓存**：首次从远程拉取后自动落盘，不预加载全量数据。
- **缓存清理（BigQuery / MaxCompute 数据源共用）**：实例化时根据 `data.cache.retention_days` 与 `data.cache.max_size_gb` 自动清理过期或溢出的本地缓存文件，避免长期累积。
- 分钟级数据时间列统一格式化为 `YYYYMMDDHHMM`（12位），便于按交易日前缀快速筛选。
- **凭据隔离**：BigQuery 服务账号通过 `GOOGLE_APPLICATION_CREDENTIALS` 环境变量或 `config/secrets.yaml` 中 `bigquery.credentials_path` 注入；MaxCompute AccessKey 仍保留为备选凭据。均不入仓库。

---

### 2.2 account/ — 账户与持仓层

| 文件 | 职责 |
|------|------|
| `position.py` | 单只股票持仓模型。核心属性：`total_qty`（总持仓）、`sellable_qty`（可卖数量）、`cost_price`（加权平均成本）。严格实现 **T+1**：通过 `_buy_records`（date→qty 映射）记录每笔买入日期，`update_sellable(current_date)` 每日开盘前解冻前一日持仓。 |
| `portfolio.py` | 虚拟账户总控。管理 `available_cash`（可用现金）、`frozen_cash`（冻结资金）、`positions`（持仓字典）。提供 `reserve_cash` / `release_cash` / `apply_buy_fill` / `apply_sell_fill` 等原子操作，保证资金流水清晰。 |

**设计要点**：
- T+1 规则在 `Position` 中显式建模，而非在 `TradeEngine` 中隐式判断，职责分离更清晰。
- 成本价采用 **加权平均法**，清仓后自动归零。

---

### 2.3 engine/ — 引擎层

| 文件 | 职责 |
|------|------|
| `trade_engine.py` | **交易撮合引擎**。职责：① 验证订单合法性（资金、T+1、涨跌停、成交量限制）；② 按 `order_type` 路由撮合（MARKET / LIMIT / STOP）；③ 计算并扣除交易费用（佣金、印花税、过户费）；④ 调用 Portfolio 更新持仓；⑤ 维护**挂单池**（`pending_orders`）——LIMIT/STOP 当根 Bar 未触发时入池，由 `sweep_pending` 在后续每根 Bar 继续检查直至成交或过期（PRD_20260520_10）。 |
| `backtest.py` | **回测主引擎**。支持日线/分钟线双频回测。按交易日历逐日（daily）或逐 Bar（1min/5min/15min/30min/60min）推进，调用策略生命周期，收集订单并交由 `TradeEngine` 撮合，记录 NAV。预加载行情时会按策略实例的 `lookback_days` 向起始日前扩展 warmup 数据，但回测记录、订单执行和净值曲线仍从用户指定起始日开始。可选逐日诊断模式会在每日收盘后生成 `DAY_SUMMARY` / `DAY_POSITION` / `DAY_CANDIDATE` 结构化日志，并把对应明细写入 `DailyRecord` 供 CSV 输出；可通过 CLI 显式传入相对沪深300超额收益阈值，在触达亏损阈值后输出 `EARLY_STOP` 并停止后续交易日，方便快速定位策略失效区间。分钟级回测中 `before_trading_start` / `after_trading_end` 仍按交易日边界调用。内置**全局止损模块**：策略 `handle_data` 执行完毕后，自动扫描持仓，当浮亏超过阈值时生成 MARKET 卖出单，与策略订单一并交由 `TradeEngine` 执行。止损对策略完全透明，无需修改任何策略代码。每根 Bar 开始时调用 `trade_engine.sweep_pending()` 扫描并尝试撮合前期挂单。 |
| `paper_trader.py` | **虚拟盘**（PRD_20260520_09）。状态持久化到 `data/paper_state.json`（含 portfolio / 策略类与构造参数 / user_data / 待执行订单 / 待止损队列），支持断点续跑。`run_once(date=T)` 完整复用回测策略循环：预加载 `[T - lookback_days, T]` 历史行情 → 用 T 日开盘价撮合上次留存订单（**next_open 语义**，与回测一致）→ 调 `before_trading_start` / `handle_data` / `after_trading_end` → 收盘后止损检查 → 持久化新订单与止损队列。重复运行同一天会被拦截。该类已在 `engine/__init__.py` 中导出。 |

**设计要点**：
- `TradeEngine` 与 `BacktestEngine` 分离：前者只负责"一笔订单能否成交"，后者负责"何时调用策略、如何组织交易日历"。
- 回测默认采用 **T+1 开盘价成交**（`price_type="next_open"`），避免未来函数（Lookahead Bias）。
- 涨跌停判定基于 `prev_close` 与当日 `open` 计算，按板块细分（PRD_20260520_08/10）：主板 ±10%、科创板 ±20%、创业板 ±20%（2020-08-24 起，之前 ±10%）、北交所 ±30%、ETF/LOF/可转债 ±10%；新股上市首 5 个交易日无涨跌幅限制（返回 1.0）；由 `utils.code.price_limit_pct(code, current_date, list_date)` 统一返回。ST ±5% 暂不支持（见 TODO）。
- 分钟级回测保持 T+1 以**交易日**为维度：当日买入的股票在当日剩余所有分钟内均不可卖，下一交易日开盘后解冻。
- **全局止损**：引擎每日/每 Bar 在策略信号之后、撮合之前自动检查持仓浮亏。仅对 `sellable_qty > 0` 的仓位生效（T+1 当日买入不会被止），止损单以 `OrderType.MARKET` 发出，与策略订单一起进入 `TradeEngine` 按 A 股规则撮合。
- **订单类型撮合规则**（详见 §4.10）：
  - `MARKET`：按 `price_type` 选 open/close，叠加滑点
  - `LIMIT`：买入 `bar.low ≤ price` 时按 price 成交；卖出 `bar.high ≥ price` 时按 price 成交；不叠加滑点
  - `STOP`：卖出 `bar.low ≤ stop_price` 触发，按 `min(open, stop_price)`（更不利价）；买入 `bar.high ≥ stop_price` 触发，按 `max(open, stop_price)`
- **挂单池与过期机制**（PRD_20260520_10，详见 §4.14）：LIMIT/STOP 未成交时进入 `TradeEngine.pending_orders`，每根 Bar 由 `sweep_pending` 扫描。支持 `time_in_force="DAY"`（当日有效）/ `"GTC"`（可指定 `expire_date` 或永不过期）。`Context.cancel_order(order_id)` 可从池中撤单。

---

### 2.4 strategy/ — 策略层

策略可以是单文件，也可以是**实验包**（推荐长期保留策略采用）。

**单文件示例**（直接放在 `strategy/` 下）：

| 文件 | 职责 |
|------|------|
| `base_strategy.py` | 策略抽象基类。定义生命周期钩子（`initialize` / `before_trading_start` / `handle_data` / `after_trading_end`）。`Context` 对象封装下单接口（`order` / `limit_order` / `stop_order`）、撤单（`cancel_order`）与数据查询（`get_price`）。`limit_order` / `stop_order` 支持 `time_in_force="DAY"/"GTC"` 与 `expire_date` 参数，并返回 `order_id` 供后续撤单。 |
| `momentum.py` | 月度动量策略示例：每月初买入上月涨幅前 N 名，等权持有。 |
| `multi_factor.py` | 多因子策略示例：基于 PE/PB/ROE 综合评分选股（演示框架，实际需接入财务数据库）。 |
| `intraday_ma.py` | 日内双均线策略示例：分钟级 MA5/MA15 金叉买入、死叉卖出 + 收盘前强制平仓。 |

**实验包结构**（推荐）：

```
strategy/<name>/
├── strategy.py           # 策略类实现（带 DEFAULT_UNIVERSE）
├── __init__.py           # 重新导出策略类（保持原 import 路径）
├── config.yaml           # preset 默认参数：universe / 时间区间 / 频率 / 基准
├── README.md             # 策略说明（信号 / 参数 / 适用市场 / 局限）
└── runs/                 # 历次回测产物（默认 gitignore，可强制保留）
```

**当前可用 preset**：

| preset | 类 | 说明 |
|---|---|---|
| `double_ma` | `strategy.double_ma.DoubleMAStrategy` | 双均线（MA5/MA20）金叉买入、死叉卖出，默认 510300.SH × 15min |
| `ml_stock_picker` | `strategy.ml_stock_picker.MLStockPickerStrategy` | LightGBM/XGBoost 日线选股，优先读取 BigQuery DWS 技术 + 估值/基本面 + 事件/资金流增强特征；模型缺失时使用确定性 fallback score |
| `ml_multi_horizon_picker` | `strategy.ml_multi_horizon_picker.MLMultiHorizonStrategy` | 多 Horizon 走步 ML 策略，读取 GCS 月度模型 registry，最多 3~5 只持仓；regime 是风险预算开关：bull 最多 5 只/85% 资金，neutral 最多 3 只/45% 资金，bear 目标 0 只/0% 资金并清掉可卖持仓 |
| `ml_multi_horizon_picker_bear_budget` | `strategy.ml_multi_horizon_picker.MLMultiHorizonStrategy` | 风险预算实验组：新手续费口径下，bear 不清仓，降为最多 2 只/30% 资金，neutral 最多 4 只/60% 资金 |
| `ml_multi_horizon_picker_crisis_budget` | `strategy.ml_multi_horizon_picker.MLMultiHorizonStrategy` | 风险预算实验组：bear 降为最多 2 只/30% 资金，并启用 `crisis` 零预算清仓层 |
| `ml_rich_picker` | `strategy.ml_rich_picker.MLRichPickerStrategy` | **富特征版**（PRD_20260525_03）：在 ml_multi_horizon 基础上把特征从 17 维扩展到 30 维（含 8 维基本面 PE/PB/ROE + 5 维资金流/龙虎榜/涨停连板）；initialize 时一次性预拉特征宽表，handle_data O(1) 查表；模型存储 `gs://.../walk_forward_rich/`，与 v1 完全隔离 |
| `bqml_signal_picker` | `strategy.bqml_signal_picker.BQMLSignalPickerStrategy` | 直接读取 BigQuery ML ADS 候选信号，动态 universe，按真实撮合引擎执行；默认 10 万资金、最多 5 只持仓、5 个交易日固定持有期 |

**preset 加载机制**：

- `python run_backtest.py --preset double_ma` → 加载 `strategy/double_ma/config.yaml`
- `python run_backtest.py --preset ml_stock_picker` → 加载 `strategy/ml_stock_picker/config.yaml`
- `python run_backtest.py --preset ml_multi_horizon_picker` → 加载 `strategy/ml_multi_horizon_picker/config.yaml`，按 `gs://data-aquarium/models/walk_forward/registry.json` 逐月切换模型
- `python run_backtest.py --preset ml_multi_horizon_picker_bear_budget` → bear 作为非零风险预算状态，用于对照测试
- `python run_backtest.py --preset ml_multi_horizon_picker_crisis_budget` → bear 非零预算 + crisis 清仓层，用于对照测试
- `python run_backtest.py --preset bqml_signal_picker` → 从 `ashare.ads_signal_ml_stock_picker_bqml_1d` 读取候选信号并真实撮合回测
- 参数优先级（高到低）：**CLI 参数 > preset config > 全局 `config/backtest.yaml` > 内置默认**
- 输出目录优先级：`--output` > `strategy/<preset>/runs/`（preset 模式）> `output/`（兜底）
- 回测成功后默认将输出目录完整归档到 `gs://data-aquarium/a-share/backtest_runs/{strategy_key}/{run_label}/`；可用 `--no-gcs-archive` 临时跳过，或用 `--gcs-archive-uri` 覆盖目标前缀。
- 长区间诊断回测可显式传入 `--early-stop-excess-vs-hs300 -0.10`，当逐日诊断里的相对沪深300超额收益低于 -10% 时提前停止并保留已生成结果；默认不启用。

**设计要点**：
- 策略与引擎完全解耦：策略只知道 `Context` 接口，不感知回测循环细节。
- 策略可通过构造函数接收参数（如 `universe / short_window`），CLI 与 preset 都可以注入。
- `Context.order` 下市价单；`Context.limit_order(code, qty, price)` 下限价单；`Context.stop_order(code, qty, stop_price)` 下止损单。
- `order_target_percent` / `order_target_value` **当前抛 NotImplementedError**（PRD_20260520_02）：自动计算会引入对最新价格的隐式依赖，易触发 Lookahead Bias，请手动计算目标数量后调用 `order()`。

---

### 2.5 analytics/ — 绩效分析层

| 文件 | 职责 |
|------|------|
| `metrics.py` | 绩效指标计算。包括累计/年化收益率、最大回撤、波动率、夏普比率、索提诺比率、Beta、Alpha、信息比率，以及**基于 FIFO 配对的胜率与盈亏比**（PRD_20260520_02）。分钟级回测时会自动把策略净值重采样到日线后再与日线基准对齐，避免因频率不匹配导致 Beta/Alpha 失真。 |
| `plotter.py` | 可视化绘图。使用 Matplotlib 生成累计收益对比图、回撤曲线、月度收益热力图。**模块加载时自动探测系统中文字体**（PingFang SC / Noto Sans CJK / Microsoft YaHei 等），避免中文渲染为方框。 |
| `report.py` | HTML 报告生成器。**完整对齐 `summary.md` 内容**（PRD_20260520_04）：策略元信息、数据来源、回测参数、交易规则、12 项绩效指标卡片、图表、交易统计、费用汇总、完整交易明细（折叠展示）。 |
| `summary.py` | Markdown 报告生成器。9 节结构：策略 / 数据 / 参数 / 规则 / 绩效 / 交易统计（含胜率盈亏比）/ 费用 / 交易明细 / 产物清单。基准未加载时显示 "n/a（基准数据未加载）"。 |
| `gcs_archive.py` | 回测产物 GCS 归档工具。成功回测后递归上传本地输出目录，并生成 `gcs_archive_manifest.json`；支持 ADC 与 `ASHARE_USE_GCLOUD_ACCESS_TOKEN=1`。 |
| `daily_diagnostics.py` | 逐日诊断 CSV 输出工具。写出 `daily_log.csv`、`daily_positions.csv`、`daily_candidates.csv`，每个 CSV 第一行中文表头、第二行英文字段名。 |

---

### 2.6 utils/ — 工具层

| 文件 | 职责 |
|------|------|
| `calendar.py` | A 股交易日历。优先调用 `chinese_calendar` 处理法定假日与调休补班（每年初需 `pip install -U chinese-calendar`）；超出范围或库缺失时降级为"非周末 + 硬编码 2020-2024 假期" + WARNING。注意：chinese_calendar 把"周末补班"标为 workday，但 A 股不在补班日开市，已二次过滤。 |
| `code.py` | **股票代码归一化**（PRD_20260520_03）：`normalize_code('510300')` → `'510300.SH'`，按前缀推断交易所；`to_exchange_code` / `to_framework_code` 处理框架格式与 MaxCompute 表内 `shXXXXXX` 格式的双向转换。统一由 CLI / data_layer / engine / strategy 共享。 |
| `logger.py` | 统一日志配置。支持控制台 + 文件双输出，UTF-8 编码。 |

---

## 3. 模块间依赖关系

```
strategy/           ← 依赖 →   engine/backtest.py (被调用)
                              data_layer/ (通过 Context.get_price)
                              account/ (通过 Context.portfolio，只读)

engine/backtest.py  ← 依赖 →   strategy/ (调用生命周期)
                              engine/trade_engine.py (委托撮合)
                              account/portfolio.py (管理资产)
                              data_layer/ (预加载行情)
                              utils/calendar.py (交易日历)

engine/trade_engine.py ← 依赖 → account/portfolio.py (更新持仓)
                                 data_layer/ (读取 bar 数据做涨跌停判定)

analytics/          ← 依赖 →   engine/backtest.py (读取 nav 结果)
                              data_layer/ (读取基准行情)

CLI (run_backtest.py) ← 依赖 → 所有模块的编排入口
```

**依赖原则**：
- **单向依赖**：上层模块可调用下层，下层不反向依赖上层。
- `strategy` 只依赖 `account` 的只读接口和 `data_layer` 的数据查询，不依赖 `engine` 的实现细节。
- `analytics` 只消费回测结果，不参与回测过程，避免循环依赖。

---

## 4. 关键设计决策

### 4.1 为什么采用 "T+1 开盘价成交" 作为默认撮合模式？

A 股回测中最容易引入 **未来函数（Lookahead Bias）** 的问题是：用当日收盘后才能确定的信号，以当日收盘价成交。

本框架默认采用 **信号日收盘后生成、下一交易日开盘价成交**（`price_type="next_open"`），这是学术界与工业界量化回测的主流做法，能最大程度避免未来函数。

如需使用收盘价成交（例如验证理论上限），可显式配置 `price_type="current_close"`，但需自行承担偏差风险。

### 4.2 为什么将 T+1 规则放在 Position 而不是 TradeEngine？

- **TradeEngine** 的职责是"判断一笔订单在当前市场条件下能否成交"，属于**市场规则**。
- **Position** 的职责是"记录我持有多少股票、哪些可以卖"，属于**账户状态**。

T+1 是 A 股对持仓状态的时间限制，而非市场价格限制，因此由 `Position` 通过 `_buy_records` 显式建模。`TradeEngine` 只需读取 `sellable_qty` 做判断，职责更清晰。

### 4.3 为什么数据层使用抽象基类 + 本地缓存？

- **可测试性**：回测引擎单元测试时，可注入 MockDataSource，无需真实网络请求。
- **性能**：AKShare 免费接口有调用频率限制，本地 Parquet 缓存可将重复回测的耗时从分钟级降到秒级。
- **可扩展性**：后续接入 Wind、Tushare Pro、自建数据库时，只需实现 `BaseDataSource` 的三个方法。

### 4.4 为什么策略的 `order_target_percent` 仅作预留？

`order_target_percent` 需要在盘中实时计算"当前总资产 × 目标比例 / 最新价"，这涉及对 Portfolio 的写操作隐式逻辑。为保持框架透明性，当前版本建议用户：

```python
# 显式计算，避免框架黑盒
price = data[code]["close"]
target_value = portfolio.total_value * 0.1
target_qty = int((target_value / price) // 100) * 100
context.order(code, target_qty - current_qty)
```

后续版本可在需求明确后补充完整实现。

### 4.5 分钟级回测是如何实现的？

框架现已支持日线/分钟线双频回测，通过 `frequency` 参数统一切换。

**数据层**：`BaseDataSource.get_bars(period="1min")` 统一封装日K与分钟K获取，`LocalStorage` 通过 `{code}_{period}.parquet` 文件名隔离不同周期缓存。

**引擎层**：`BacktestEngine.run()` 根据 `frequency` 自动分发到 `_run_daily()` 或 `_run_intraday()`。
- 日线模式：每个交易日调用一次 `handle_data()`。
- 分钟模式：每个交易日加载当日全部分钟 Bar，按时间顺序逐条推进；`before_trading_start` 仅在 9:30 第一个 Bar 调用，`after_trading_end` 仅在 15:00 最后一个 Bar 调用。

**T+1 兼容**：分钟级回测中 T+1 仍以**交易日**为解冻维度，而非逐分钟解冻。当日任意时刻买入的股票，当日剩余分钟内 `sellable_qty = 0`；下一交易日 9:30 第一个 Bar 之前执行 `portfolio.before_trading()` 统一解冻。

**绩效分析**：`metrics.py` 根据 `frequency` 自动选择年化系数（日线 252，1min 252×240），避免分钟级收益率使用日线年化系数导致指标失真。

### 4.6 为什么默认数据源切换为 BigQuery？

历史上本框架默认使用 AKShare（免费）+ Tushare Pro（备选），后迁移至阿里云 MaxCompute。随着数据规模扩大与 GCP 生态整合需求，项目所有者已将数据仓库迁移至 Google Cloud BigQuery（项目 `data-aquarium`，单 dataset `ashare`，通过表前缀 `ods_` / `dwd_` / `dws_` / `ads_` 表达数据分层）。BigQuery 提供标准 SQL、列式存储与分区裁剪能力，适合作为长期研究和回测数据仓库。

截至 PRD_20260524_06，数据迁移处于 **单 dataset 已确认、当前 GCS Parquet 作为唯一正式输入源、ODS/DWD/DWS/ADS 已覆盖旧表和新增 raw 标准化表并通过审计，财务指标和基本面特征专项修复完成，新增事件/资金流 DWS/ADS 已生成** 的状态：

- GCS Parquet 已完成：`gs://data-aquarium/a-share/standardized_parquet/`。
- 新增 GCS raw 目录已通过 VM 标准化并写入当前正式 GCS prefix，新增表包括 `fact_kpl_board_1d`、`fact_dragon_tiger_seat_1d`、`fact_money_flow_1d`、`dim_index_profile`、`fact_index_component_1d`、中信/申万行业维表、行业成分、行业行情和指数市场指标表。
- BigQuery ODS：基于当前 GCS prefix 创建 `ashare.ods_*` external table，覆盖 manifest 中 48 张源表、17,697 个对象；不复制 ODS 业务数据。
- ODS external manifest：`ashare.ods_external_manifest` 已同步当前 GCS 对象清单。
- BigQuery DWD：`ashare.dwd_*` native table 已覆盖 48 张源表并通过 `audit-dwd`；其中 `ashare.dwd_fact_financial_indicator` 已从 GCS 原始 Parquet 直接修复重建，341,977 行，`equity_code` 341,977 行非空，`eps_basic` 309,236 行非空，`bps` 328,544 行非空，`roe` 335,752 行非空。新增表中 `dwd_fact_money_flow_1d` 13,602,167 行、`dwd_fact_dragon_tiger_seat_1d` 1,811,517 行、`dwd_fact_kpl_board_1d` 235,147 行。
- BigQuery DWD core：新增 `ashare.dwd_fact_income_statement_core`（317,841 行）和 `ashare.dwd_fact_balance_sheet_core`（311,277 行），从当前 GCS Parquet 真实中文字段抽取利润表/资产负债表关键指标，不覆盖原泛化 DWD 表。
- BigQuery DWS：`ashare.dws_*` 第一版策略特征层已生成 6 张表，并新增 `ashare.dws_equity_valuation_features` 估值特征表（16,275,314 行）、`ashare.dws_equity_fundamental_features` 基本面特征表（16,275,470 行）和 `ashare.dws_equity_event_money_flow_features_1d` 事件/资金流特征表（13,612,139 行）。
- BigQuery ADS：`ashare.ads_*` 第一版策略信号层已生成 5 张表，并新增 `ashare.ads_signal_event_money_flow_1d`（13,612,139 行，每日最多 100 个候选）与 `ashare.ads_signal_ml_stock_picker_bqml_1d`（BigQuery ML baseline 候选信号），通过对应 audit。

因此，`BigQueryDataSource` 是当前默认数据源实现，日线回测可直接读取 `ashare.dwd_*` 标准表。DWS/ADS 主要作为特征与信号候选表存在；其中 `ml_stock_picker` 已通过 `BigQueryDataSource.get_equity_feature_snapshot()` / `get_equity_feature_history()` 可选读取 `ashare.dws_*` 增强特征并在策略内排序下单；`bqml_signal_picker` 已通过 `BigQueryDataSource.get_bqml_signal_candidates()` 直接读取 `ashare.ads_signal_ml_stock_picker_bqml_1d` 候选信号，并交由真实撮合引擎执行。

**实现要点**：
- `BigQueryDataSource` 实现 `BaseDataSource` 全部三个抽象方法。
- `BigQueryDataSource` 额外提供可选 DWS/ADS helper：`get_equity_feature_snapshot(codes, date, feature_set)` 供回测调仓日读取截面特征；`get_equity_feature_history(codes, start, end, feature_set, label_horizon)` 供 ML 训练脚本读取增强特征并生成训练标签；`get_bqml_signal_candidates(start_date, end_date, table_name, candidate_pool_size)` 供 BQML ADS 信号策略读取候选池。这些方法不属于 `BaseDataSource` 抽象接口。
- `BigQueryDataSource` 接收 `trading_permissions` 配置；`get_stock_list(as_of_date=...)` 可按上市/退市日期返回指定信号日可交易股票，`include_inactive=True` 可返回全历史股票池供 rich 预加载使用；历史 as-of 模式不使用当前 `security_name` 的 ST/退市字样过滤，避免幸存者偏差，严格 ST PIT 过滤需后续接入历史 ST 状态；`get_bqml_signal_candidates()` 会 join `dwd_dim_security` 并过滤当前账户无权限标的，`get_bars()` / `get_multi_bars()` 也会用代码前缀拦截直接传入的北交所、科创板、创业板股票。
- `BigQueryDataSource.get_multi_bars(codes, start, end, period="daily")` 对股票日线走 BigQuery 批量查询，减少 ADS 候选池回测时的串行 per-code 查询开销；其他周期或资产类型仍回退到逐标的 `get_bars()`。
- 连接懒加载，凭据通过 `GOOGLE_APPLICATION_CREDENTIALS` 环境变量或 `config/secrets.yaml`（gitignored）注入；本地开发可设置 `ASHARE_USE_GCLOUD_ACCESS_TOKEN=1` 临时复用 `gcloud auth print-access-token`。
- 本地 Parquet 缓存与清理策略由 `data.cache.retention_days` / `data.cache.max_size_gb` 控制，避免重复查询计费；K 线缓存 key 包含完整证券代码与 `adjust_type`，避免 `qfq/none/hfq` 串缓存。
- `maxcompute_source.py`、`akshare_source.py` 与 `tushare_source.py` 保留但不再被 `run_backtest.py` 默认装载，以便后续按需切换。
- GCS 到 BigQuery 的 ODS 接入由 `gcs_to_bigquery/pipeline.py` 负责，包括 manifest、ODS external table 创建、ODS audit 和历史 staging/load 兼容命令。
- 新增 raw 到标准化 Parquet 的 VM 处理仍由 `data_transfer/prepare_parquet_to_gcs.py` 负责。`sync-raw` 命令只读 GCS raw，并按 per-prefix suffix 规则同步到 `/mnt/localssd/raw_incremental/...`，随后复用 `build` / `upload` / `audit` 命令生成并上传 month-partitioned Parquet；普通指数日/周/月 K 线全量和普通指数日线增量已从新增 raw 任务中排除，避免与现有 `fact_index_kline_*` 重复。
- BigQuery 内部 DWD/DWS/ADS 加工由 `bigquery_pipeline/` 负责。当前入口已承接完整 DWD transform/audit、DWS/ADS 策略特征与信号层、BigQuery ML baseline、`fact_financial_indicator` 专项修复、利润表/资产负债表 core 抽取、估值/基本面特征和事件/资金流特征/信号生成；后续新的 BigQuery 内部加工不得继续放入 `gcs_to_bigquery`。
- `gcs_to_bigquery` 默认使用 Google Application Default Credentials；`auth.use_gcloud_access_token` 或环境变量 `ASHARE_USE_GCLOUD_ACCESS_TOKEN=1` 可作为 fallback，且 gcloud token 支持超时与刷新。
- `gcs_to_bigquery` 本地 manifest 默认写入 `${HOME}/.local/state/ashare/ods_pipeline_manifest.jsonl`，不再写入 `/tmp`。
- `bigquery_pipeline` 同样支持 `ASHARE_USE_GCLOUD_ACCESS_TOKEN=1`，配置文件为 `bigquery_pipeline/config.yaml`。

### 4.7 BigQuery 表结构与接入进度

> **当前状态**（截至 PRD_20260524_06）：`ashare.ods_*` external table 覆盖 48 张源表，`ashare.dwd_*` native table 覆盖 48 张源表并通过审计；`ashare.dwd_fact_financial_indicator` 已绕过坏 external schema 直接由 GCS Parquet 修复重建；利润表/资产负债表已新增窄 DWD core 表；`ashare.dws_*` / `ashare.ads_*` 第一版已生成，估值、基本面和事件/资金流特征与候选信号均已进入 BigQuery。

| 用途 | 配置键（`config/backtest.yaml`） | 状态 | 备注 |
|------|----------------------------------|------|------|
| 股票日K线 | `data.bigquery.tables.kline_1d_equity` | ✅ **可用** | `ashare.dwd_fact_equity_kline_1d`，股票事实代码字段为 `equity_code` |
| 基金日K线 | `data.bigquery.tables.kline_1d_fund` | ✅ **可用** | `ashare.dwd_fact_fund_kline_1d` |
| 指数日K线 | `data.bigquery.tables.kline_1d_index` | ✅ **可用** | `ashare.dwd_fact_index_kline_1d`；指数 `000/930/932/950` 规范为 `.SH`，`399` 规范为 `.SZ` |
| 股票列表 | `data.bigquery.tables.dim_security` | ✅ **可用** | `ashare.dwd_dim_security`，多资产维表保留 `security_code` |
| 指数成分股 | `data.bigquery.tables.board_component` | ✅ **可用** | `ashare.dwd_fact_board_component_1d`，成分股字段为 `equity_code` |
| 财务指标 | 暂未接入默认 datasource 配置 | ✅ **可用** | `ashare.dwd_fact_financial_indicator`，股票字段为 `equity_code`，财报可见日期为 `announcement_date` |
| 估值特征 | `data.bigquery.tables.dws_equity_fundamental_features` | ✅ **可用** | ML 选股从 `ashare.dws_equity_fundamental_features` 读取 `pe_basic` / `pb` / `roe` 等增强特征；独立轻量表 `dws_equity_valuation_features` 仍保留 |
| 基本面特征 | `data.bigquery.tables.dws_equity_fundamental_features` | ✅ **可用** | `ashare.dws_equity_fundamental_features`，合并财务指标、利润表、资产负债表和日行情，按各来源 `announcement_date <= date` 生效 |
| 开盘啦榜单 | 暂未接入默认 datasource 配置 | ✅ **可用** | `ashare.dwd_fact_kpl_board_1d`，并汇入 `dws_equity_event_money_flow_features_1d` |
| 龙虎榜席位 | 暂未接入默认 datasource 配置 | ✅ **可用** | `ashare.dwd_fact_dragon_tiger_seat_1d`，并汇入事件/资金流 DWS/ADS |
| 资金流向 | 暂未接入默认 datasource 配置 | ✅ **可用** | `ashare.dwd_fact_money_flow_1d`，并汇入事件/资金流 DWS/ADS |
| 指数成分 | 暂未接入默认 datasource 配置 | ✅ **可用** | `ashare.dwd_fact_index_component_1d`；不复用 `fact_board_component_1d` |
| 中信/申万行业分类与行情 | 暂未接入默认 datasource 配置 | ✅ **可用** | `ashare.dwd_dim_citic_industry`、`ashare.dwd_dim_sw_industry`、`ashare.dwd_fact_citic_industry_kline_1d`、`ashare.dwd_fact_sw_industry_kline_1d`、`ashare.dwd_fact_sw_industry_component_1d` |
| 大盘指数每日指标 | 暂未接入默认 datasource 配置 | ✅ **可用** | `ashare.dwd_fact_index_market_indicator_1d`；普通指数 K 线仍沿用现有 `fact_index_kline_*` |
| 事件/资金流特征 | `data.bigquery.tables.dws_equity_event_money_flow_features_1d` | ✅ **可用** | ML 选股可读取 `ashare.dws_equity_event_money_flow_features_1d`；`ashare.ads_signal_event_money_flow_1d` 仍为候选信号层 |
| BQML 选股候选信号 | `data.bigquery.tables.ads_signal_ml_stock_picker_bqml_1d` | ✅ **可用** | `ashare.bqml_ml_stock_picker_baseline` 基于 DWS 增强特征训练，`ashare.ads_signal_ml_stock_picker_bqml_1d` 每日最多 50 个候选；`bqml_signal_picker` 可直接读取 ADS 信号并交由真实撮合引擎下单 |
| 复权因子 | `data.bigquery.tables.adjust_factor` | 🟡 接口预留 | 日K表已内置复权，单独复权因子表待按需启用 |
| 1min K | `data.bigquery.tables.kline_1min_equity` | ❌ 待建表 | 调用时抛 `NotImplementedError` |
| 5min K | `data.bigquery.tables.kline_5min_equity` | ❌ 待建表 | 调用时抛 `NotImplementedError` |
| 15min K | `data.bigquery.tables.kline_15min_equity` | ❌ 待建表 | 调用时抛 `NotImplementedError` |
| 30/60min K | `data.bigquery.tables.kline_30/60min_equity` | ❌ 待建表 | 调用时抛 `NotImplementedError` |

后续每一项分钟级表接入时，对应方法 `_fetch_minute_bars` 内分支需按实际字段名拼接 SQL，列名映射到框架标准列 `[code, date, open, high, low, close, volume, amount]`。

### 4.8 BigQuery 日K线表接入细节

**目标 dwd 表结构**（PRD_20260522_01 / PRD_20260523_05 / PRD_20260523_07 规范）：

| 列 | 类型 | 含义 |
|---|---|---|
| `date` | DATE | K 线日期 |
| `partition_month` | INT64 | **分区字段**，格式 `YYYYMM` |
| `equity_code` | STRING | 股票代码，**标准格式 `XXXXXX.SH` / `XXXXXX.SZ` / `XXXXXX.BJ`** |
| `source_code` | STRING | 原始代码（备用） |
| `adjust_type` | STRING | 复权类型：`none` / `qfq` / `hfq` |
| `open / high / low / close` | NUMERIC | OHLC |
| `volume` | NUMERIC | 成交量（股） |
| `amount` | NUMERIC | 成交额（元） |
| `amplitude / pct_change / change / turnover_rate` | NUMERIC | 涨跌幅等（框架不全部返回） |

**关键约定**：

1. **代码格式统一**：BigQuery 表内直接使用框架标准格式 `XXXXXX.SH` / `XXXXXX.SZ` / `XXXXXX.BJ`，**无需像 MaxCompute 那样做 shXXXXXX 双向映射**。股票事实表使用 `equity_code`，指数事实表使用 `index_code`；指数代码中 `000/930/932/950` 前缀归一为 `.SH`，`399` 前缀归一为 `.SZ`。
2. **分区裁剪**：`get_bars()` 根据请求的 `[start_date, end_date]` 计算覆盖的 `partition_month` 列表（`_partition_months_in_range()`），SQL 中以 `partition_month IN (202001, 202002, ...)` 触发分区裁剪。**不裁剪则全表扫描，查询成本显著增加。**
3. **复权字段约定**：目标日K表通过 `adjust_type` 字段区分 none/qfq/hfq。回测撮合、资金、费用与 NAV 默认使用 `data.execution_adjust_type: none`；策略训练/特征可单独使用 qfq。当前 BigQuery qfq 只能说明数据口径，不能单独证明 point-in-time；若要严格 PIT，需要补采 Tushare `stk_factor` 快照或按 `daily none + adj_factor` 自建 as-of 复权序列。
4. **资产类型自动路由**：`BigQueryDataSource._resolve_kline_table()` 根据代码前缀自动判断：
   - ETF/LOF（51/56/58/11.SH, 15/16.SZ）→ `fact_fund_kline_1d`
   - 指数（000/399/930/950 前缀）→ `fact_index_kline_1d`
   - 其他 → `fact_equity_kline_1d`
5. **账户权限过滤**：`config/backtest.yaml` 的 `account.trading_permissions` 默认将北交所、科创板、创业板、港股通、新三板、ST/退市整理、融资融券、期权、可转债、CDR、未知证券信息权限均设为 `false`。当前实际影响股票策略的是北交所、科创板、创业板、ST/退市整理和未知证券过滤；融资融券、期权、可转债等先作为账户能力声明，后续接入对应资产或下单能力时复用。
6. **数据时间覆盖**：日K线数据覆盖范围由数据迁移 PRD 决定，超出范围的请求返回空 DataFrame，不报错。
7. **ods 到 dwd 的红线**：`ashare.ods_*` 中的中文源字段不能直接进入回测读取路径；必须经显式字段映射、类型转换、主键去重和 `audit-dwd` 验收后，才允许写入 `ashare.dwd_*`。

### 4.10 订单类型撮合规则（PRD_20260520_06）

`engine.trade_engine.TradeEngine._try_fill` 按 `order.order_type` 分支处理：

| OrderType | 触发条件（买入） | 触发条件（卖出） | 成交价 | 是否叠加滑点 |
|---|---|---|---|---|
| `MARKET` | 总是 | 总是 | `next_open` 模式取 `bar.open`，`current_close` 模式取 `bar.close` | ✓ |
| `LIMIT` | `bar.low ≤ order.price` | `bar.high ≥ order.price` | `order.price` | ✗（限价单自带价格约束）|
| `STOP` | `bar.high ≥ order.stop_price` | `bar.low ≤ order.stop_price` | 买入：`max(bar.open, stop_price)`；卖出：`min(bar.open, stop_price)`（保守取更不利价）| ✗ |

涨跌停判定（按板块取自 `utils.code.price_limit_pct`）与成交量限制（`volume_limit`）对所有订单类型均生效。详见 §4.13。

策略侧用法：

```python
context.order(code, qty)                            # MARKET
oid = context.limit_order(code, qty, price=10.0)    # LIMIT，返回 order_id
oid = context.stop_order(code, -qty, stop_price=9.5)  # STOP（卖出止损），返回 order_id

# GTC 限价单（持续有效至 20240131 到期）
oid = context.limit_order(code, qty, price=9.8,
                           time_in_force="GTC", expire_date="20240131")
# 主动撤单
context.cancel_order(oid)
```

### 4.11 基准加载策略（PRD_20260520_03）

`BacktestEngine._load_benchmark()` 优先按 **`period="daily"`** 加载基准行情：

- 基准只用于收益对比与 Beta/Alpha 计算，日线精度足够
- 分钟级指数表当前未建，强制用 frequency 会触发 NotImplementedError 被静默吞掉，导致 metrics 失真

若 daily 表未配置则**降级**到回测频率作 fallback。两路都失败时打 WARNING 并提示用户基准 `benchmark_return / Beta / Alpha` 将为 0。

`analytics.metrics.calculate_metrics` 中：当 `frequency != "daily"` 且基准为日线时，策略 nav 会自动重采样到日线再对齐 benchmark；并要求至少 20 个对齐日线点才计算 Beta，否则保持 0。

### 4.12 单元测试（PRD_20260520_07 / 08）

`tests/` 目录覆盖核心模块的边界场景：

| 测试文件 | 覆盖模块 | 关键用例 |
|---|---|---|
| `test_position.py` | `Position` | T+1 解冻、FIFO 同步消减 _buy_records（防 sellable_qty 虚高）、清仓归零 |
| `test_portfolio.py` | `Portfolio` | 冻结/释放、买入扣 frozen、卖出回笼 cash |
| `test_trade_engine.py` | `TradeEngine` | 佣金最低限、涨跌停拦截（含板块细分）、成交量截断、T+1、LIMIT/STOP 撮合、Order qty 警告；**挂单池**（AC-9.1~9.7：入池、跨 Bar 触发、DAY 过期、GTC 跨日保留/expire_date 过期、cancel）|
| `test_code.py` | `utils.code` | 代码归一化、前缀推断、未知交易所抛错 |
| `test_metrics.py` | `analytics.metrics._pair_fifo` + `calculate_metrics` | 全盈/全亏/混合配对、未平仓忽略、FIFO 顺序、inf 盈亏比 |
| `test_price_limit.py` | `utils.code.price_limit_pct` | 主板 / 科创板 / 创业板（含 2020-08-24 切换）/ 北交所 / 新股首日（前 5 交易日 ±100%）/ ETF / 异常输入 fallback |
| `test_paper_trader.py` | `PaperTrader`（PRD_20260520_09）| 首次启动校验、state 往返序列化、老 state 向后兼容、next_open 延迟成交、user_data 跨日续接、重复运行拦截、止损队列持久化、user_data 不可序列化报错 |

### 4.13 板块涨跌停规则（PRD_20260520_08 / PRD_20260520_10）

`utils.code.price_limit_pct(code, current_date, list_date="")` 统一按代码前缀返回涨跌停比例：

| 代码模式 | 板块 | 涨跌幅 |
|---|---|---|
| `60xxxx.SH` | 沪市主板 | 0.10 |
| `000/001/002/003xxxx.SZ` | 深市主板（含原中小板） | 0.10 |
| `688/689xxxx.SH` | 科创板 | 0.20 |
| `300/301xxxx.SZ` | 创业板 | 0.20*（2020-08-24 起；之前 0.10）|
| `43/83/87/88/92xxxx.BJ` | 北交所 | 0.30 |
| `51/56/58/11xxxx.SH` / `15/16xxxx.SZ` | ETF / LOF / 可转债 | 0.10 |
| 其他 / 异常输入 | fallback | 0.10 |
| **任意板块，上市首 5 个交易日**（`list_date` 非空） | 新股无涨跌幅限制 | **1.0**（等效 ±100%）|

新股首日规则优先级最高，在所有板块分支之前判断。`TradeEngine._try_fill` 取 `list_date` 的优先级：`order.list_date` > `engine.listing_dates[code]` > `""`（不启用新股规则）。

`_parse_date` 兼容 `YYYYMMDD` / `YYYY-MM-DD` / `YYYY/MM/DD` / `YYYYMMDDHHMM` 格式。

**未来扩展**（TODO 已记录）：ST/*ST ±5%（缺数据源）。

### 4.14 虚拟盘策略循环（PRD_20260520_09）

`PaperTrader.run_once(date=T)` 与回测引擎共享同一套策略生命周期、撮合时序与止损模块。完整流程：

```
1. load_state()                       ← state.json 含 portfolio / 策略类与 kwargs /
                                       user_data / pending_orders / pending_stop_loss
2. 实例化策略 strategy_cls(**strategy_kwargs)
3. 预加载 [T - lookback_days, T] 历史行情 → context.all_bars
4. portfolio.before_trading(T)        ← 解冻 T-1 买入的股票
5. 用 T 日开盘价撮合 pending_orders + pending_stop_loss（next_open 语义）
6. strategy.before_trading_start(T) / handle_data(T) / after_trading_end(T)
7. 收盘后止损检查 → 新的 pending_stop_loss
8. save_state()                       ← 新 pending_orders / pending_stop_loss / user_data
```

**关键设计**：

- **next_open 语义对齐回测**：T 日 `handle_data` 产生的订单不在 T 日成交，而是写进 `pending_orders` 持久化，等下次 `run_once(T+1)` 用 T+1 开盘价成交。这与回测引擎 `_run_daily` 完全一致，便于"虚拟盘 vs 回测"对比验证。
- **策略元信息持久化**：`strategy_class`（模块路径字符串）+ `strategy_kwargs` 写入 state.json，下次启动只需传 `state_file` 即可恢复。外部传入的 `strategy_cls` / `strategy_kwargs` 优先级最高，便于切换策略或修改参数。
- **lookback_days 协议**：`BaseStrategy.lookback_days`（默认 60）声明策略需要多少天历史 bar 来 warm up 指标。`DoubleMAStrategy` 覆盖为 `long_window + 10`。PaperTrader 按 `lookback_days × 1.6` 估算自然日跨度预加载行情。
- **重复运行拦截**：`last_run_date >= date` 时本次 `run_once` 直接跳过并 WARNING，防止误重跑覆盖状态。
- **user_data 序列化约定**：必须是 JSON 兼容类型；保存时遇到不可序列化对象直接 TypeError，**不静默丢失**。

**首次启动 vs 续跑**：

| 场景 | strategy_cls 是否必需 |
|---|---|
| 首次启动（无 state.json） | 必需（否则 ValueError） |
| 续跑（state.json 存在） | 可选；不传则从 state 反射；传入则覆盖 |

运行：`pytest tests/ -v`（当前 247 个用例，期望 245 passed / 2 skipped）。开发依赖见 `requirements-dev.txt` 与策略扩展依赖见 `requirements.txt`。

### 4.15 挂单池、过期与取消机制（PRD_20260520_10）

**背景**：原始实现中 LIMIT/STOP 订单在当根 Bar 未触发时直接丢弃，导致策略无法使用"挂单等待触发"语义。PRD_20260520_10 引入完整的订单生命周期管理。

**核心数据结构**：

```
TradeEngine.pending_orders: Dict[str, Order]   # order_id → Order
Order.order_id: str   # uuid4 hex[:12]，构造时自动生成
Order.time_in_force: str   # "DAY"（当日有效）/ "GTC"（持续有效）
Order.expire_date: Optional[str]   # YYYYMMDD；仅 GTC 单可设置；None = 永不过期
```

**订单流转路径**：

```
handle_data → Context.limit_order / stop_order
    → Context._orders（本 Bar 新单队列）
        → TradeEngine.execute_orders（当根 Bar 尝试一次）
            → 触发 → Fill → Portfolio 更新
            → 未触发 → add_pending → pending_orders 池
                → 每根 Bar: sweep_pending
                    → 触发 → Fill → 从池中移除
                    → 未触发 → _is_expired?
                        → DAY + is_last_bar_of_day → 过期移除
                        → GTC + current_date > expire_date → 过期移除
                        → 否则保留
```

**过期判定规则**（`TradeEngine._is_expired`）：

| time_in_force | 过期条件 |
|---|---|
| `DAY` | `is_last_bar_of_day == True`（日线回测恒为 True） |
| `GTC`（无 expire_date） | 永不因时间过期 |
| `GTC`（有 expire_date） | `current_date[:8] > expire_date` |

**资金预留（sweep_pending 买单）**：

1. 调用 `portfolio.reserve_cash(est_amount)` 预留（LIMIT→limit price；STOP→max(open, stop_price)；其他→open）
2. 若 reserve 失败（资金不足）：本 Bar 跳过撮合，但保留挂单（若同时到期则移除）
3. 若 `_try_fill` 失败（未触发）：`release_cash(est_amount)` 退回；随后检查过期
4. 若成交：`_apply_fill` 消耗 frozen cash；挂单从池中移除

**撤单 API**（`Context.cancel_order(order_id)`）：

- 先在本 Bar 的 `_orders` 队列中查找（当 Bar 新下的、未送往引擎的单子）
- 再通过 `_cancel_callback`（注入的 `TradeEngine.cancel`）查找挂单池
- 找到 → True；未找到/已成交/已过期 → False

**BacktestEngine 集成**：

- `_run_daily`：每个交易日在 `before_trading_start` 后、`execute_orders` 前调用 `sweep_pending(..., is_last_bar_of_day=True)`（日线每天只有一根 Bar）
- `_run_intraday`：每根分钟 Bar 调用 `sweep_pending(..., is_last_bar_of_day=is_last_bar_of_day)`，复用已有的 `is_last_bar_of_day` 变量
- sweep 返回的 `Fill` 列表与 `execute_orders` 返回的合并后一起写入 `DailyRecord.fills`

---

### 4.9 ETF 15min 表 (`cn_etf_kline_15min`) 接入细节

**表结构**（PRD_20260519_03 调研结果）：

| 列 | 类型 | 含义 |
|---|---|---|
| `trade_time` | DATETIME | K 线时间，精确到分钟 |
| `code` | STRING | ETF 代码，**表内格式已是框架格式 `XXXXXX.SH` / `XXXXXX.SZ`**，与 5min 表不同 |
| `name` | STRING | ETF 中文名 |
| `open / close / high / low` | DOUBLE | OHLC |
| `volume` | BIGINT | 成交量（股） |
| `amount` | DOUBLE | 成交额（元） |
| `change_pct / amplitude` | DOUBLE | 涨跌幅 / 振幅（框架不返回） |
| `year_month` | STRING | **分区字段**，格式 `YYYYMM` |

**数据覆盖**：分区范围 200502 ～ 202512，共 251 个分区。  
**510300.SH**（沪深300ETF华泰柏瑞）数据起始：2012-07。

**与 5min 表的关键区别**：

| 维度 | cn_stock_kline_5min | cn_etf_kline_15min |
|---|---|---|
| 标的类型 | A 股普通股票 | ETF 基金 |
| 代码格式 | 表内 `shXXXXXX`，需双向映射 | 已是框架格式，**无需映射** |
| 时间粒度 | 5 分钟 | 15 分钟 |
| 分区键 | `year_month` | `year_month` |
| 全表扫描 | 被禁止，需带分区条件 | 同左 |

**实现**：`_fetch_etf_15min_bars()` — code 参数直接透传至 SQL，返回时亦原样保留。

### 4.16 BigQuery 数据分层与字段映射（PRD_20260523_06/09）

**数据分层架构**（单 dataset `ashare` + 表名前缀）：

| 层级 | 前缀 | 职责 |
|------|------|------|
| ODS | `ods_` | 贴源 external table，保留 GCS Parquet 原始 schema 和中文字段，不重复存储业务数据 |
| DWD | `dwd_` | 标准字段层，英文字段名、严格类型、主键去重、分区聚簇 |
| DWS | `dws_` | 汇总/特征层，当前已生成股票/基金/指数日线特征、组合收益基础表、板块最新成分、配对候选统计、股票估值特征、股票基本面特征和事件/资金流特征 |
| ADS | `ads_` | 应用/信号层，当前已生成双均线、ML 选股 proxy、波动率择时、市场状态 proxy、组合风险快照、事件/资金流候选信号和 BigQuery ML 候选信号 |

**字段映射配置**：

- `gcs_to_bigquery/config.yaml`：保留 ODS external table、GCS prefix、历史 load/staging 配置和旧字段映射配置。
- `bigquery_pipeline/config.yaml`：承接 BigQuery 内部加工配置，包含 project、location、dataset、当前正式 GCS prefix、`fact_financial_indicator` 和 fundamental 并行读取参数。
- `field_mappings.common`：通用中英字段候选映射（如 `股票代码 → equity_code`）
- `field_mappings.per_table.<table>.code_column`：DWD 目标代码字段
- `field_mappings.per_table.<table>.source_candidates`：ODS 可能存在的源字段候选列表
- `financial_date_policy.strict_visible_date`：财务表可见日期来源（`announcement_date`）
- `financial_date_policy.report_period_is_not_visible_date`：禁止用 `report_period` 作为可见日期

**资产代码字段命名规则**：

| 表类型 | DWD 字段 | 说明 |
|--------|----------|------|
| 股票事实表 | `equity_code` | `fact_equity_kline_1d`, `fact_adjust_factor`, `fact_limit_price_1d`, `fact_suspend_1d`, `fact_st_status_1d` |
| 基金事实表 | `fund_code` | `fact_fund_kline_1d` |
| 指数事实表 | `index_code` | `fact_index_kline_1d` |
| 板块事实表 | `board_code` | `fact_board_kline_1d` |
| 板块成分表 | `board_code` + `equity_code` | `fact_board_component_1d` |
| 多资产维表 | `security_code` | `dim_security`（多资产统称，不改为 `equity_code`） |

**北交所代码规范化**（`normalize_security_code`）：

| 输入模式 | 输出 | 说明 |
|----------|------|------|
| `430xxx` / `83xxxx` / `87xxxx` / `88xxxx` / `920xxx` | `XXXXXX.BJ` | 北交所 |
| `5xxxxx` / `6xxxxx` / `9xxxxx`（非 92） | `XXXXXX.SH` | 沪市 |
| 其他 6 位数字 | `XXXXXX.SZ` | 深市 |
| `SH/SZ/BJ` + 6 位 | `XXXXXX.EX` | 交易所前缀格式 |

**指数代码规范化**（DWD SQL `normalize_index_code_sql` / Python `normalize_index_code`）：

| 输入模式 | 输出 | 说明 |
|----------|------|------|
| `000xxx` / `930xxx` / `932xxx` / `950xxx` | `XXXXXX.SH` | 沪市/中证指数代码 |
| `399xxx` | `XXXXXX.SZ` | 深市指数代码 |
| `000300.SZ` | `000300.SH` | 源数据若带不适合指数语义的 dotted suffix，DWD 指数表会按指数规则重写 |

**DWD 转换辅助函数**：

- `gcs_to_bigquery/pipeline.py` 仅作为 ODS/GCS 管道和历史兼容代码保留，不再作为 DWD/DWS/ADS 生产入口。
- `bigquery_pipeline/sql.py`：`normalize_security_code(value)`、日期解析、SQL quote/table helper。
- `bigquery_pipeline/financial.py`：直接读取 GCS Parquet 的真实中文字段，生成 `dwd_fact_financial_indicator`，并生成 `dws_equity_valuation_features`。
- `bigquery_pipeline/fundamental.py`：直接读取 GCS Parquet 的真实中文字段，生成 `dwd_fact_income_statement_core`、`dwd_fact_balance_sheet_core`，并生成 `dws_equity_fundamental_features`。
- `bigquery_pipeline/dwd.py`：DWD 层审计入口，财务指标审计必须校验 `equity_code`、`announcement_date`、`report_period` 及核心指标非空。
- `bigquery_pipeline/dws.py`：DWS 层转换/审计入口，目前包含 `equity_valuation_features` 与 `equity_fundamental_features`。
- `bigquery_pipeline/ads.py`：ADS 层候选信号生成与 audit。
- `bigquery_pipeline/bqml.py`：BigQuery ML baseline 训练、预测和 audit。训练使用 DWS 增强特征，模型表为 `ashare.bqml_ml_stock_picker_baseline`，预测写入 `ashare.ads_signal_ml_stock_picker_bqml_1d`。

**BigQuery 内部管道命令**（`bigquery_pipeline/cli.py`）：

- `repair-financial-indicator`：从当前正式 GCS Parquet 直接重建 `ashare.dwd_fact_financial_indicator`。
- `audit-financial-indicator`：校验财务 DWD 行数和关键字段非空。
- `transform-equity-valuation-features` / `audit-equity-valuation-features`：生成并验收 `ashare.dws_equity_valuation_features`。
- `repair-fundamental-inputs` / `audit-fundamental-inputs`：生成并验收利润表/资产负债表窄 DWD core 表。
- `transform-equity-fundamental-features` / `audit-equity-fundamental-features`：生成并验收 `ashare.dws_equity_fundamental_features`。
- `transform-dwd` / `audit-dwd`：DWD 层入口；当前活动重建路径为 `fact_financial_indicator`。
- `transform-dws` / `audit-dws`：DWS 层入口；当前活动新增表为 `equity_valuation_features` 和 `equity_fundamental_features`。
- `train-bqml-ml-stock-picker` / `predict-bqml-ml-stock-picker` / `audit-bqml-ml-stock-picker`：训练 BigQuery ML 选股 baseline、生成 ADS 候选信号并验收模型指标与 Top-N 约束。日常日线更新后只需要跑预测；训练按周/月滚动或在特征 schema/市场环境明显变化时触发。
- `bqml_signal_picker` 是当前唯一直接读取 ADS 候选信号并回测下单的策略；其回测口径已由 PRD_20260524_10 约束，仍使用 `BacktestEngine` / `TradeEngine` 的 next-open、费用、滑点、涨跌停、成交量限制和 T+1 规则。

---

## 5. 扩展指南

### 5.1 接入新数据源

1. 继承 `BaseDataSource`，实现 `get_bars`、`get_stock_list`、`get_index_constituents`。
2. 在 `run_backtest.py` 中替换 `build_bigquery_data_source()` 的调用为你的实现，或在 `data.source` 配置项基础上扩展分支。
3. 参考 `bigquery_source.py` 的缓存策略、分区裁剪与资产类型路由设计。

### 5.2 添加新策略

1. 在 `strategy/` 下新建 `.py` 文件。
2. 继承 `BaseStrategy`，重写 `initialize` 和 `handle_data`。
3. 运行：`python run_backtest.py --strategy strategy.your_module.YourStrategy`

### 5.3 修改交易规则

编辑 `config/backtest.yaml`：
- `trading.commission_rate`：佣金费率；当前默认万一（`0.0001`）
- `trading.min_commission`：最低佣金；当前默认免五（`0.0`）
- `data.execution_adjust_type`：回测撮合 / NAV 行情复权口径；当前默认 `none`
- `slippage.value`：滑点大小
- `execution.price_type`：`next_open` 或 `current_close`
- `stop_loss.enabled`：是否启用全局止损
- `stop_loss.threshold`：止损阈值（如 `0.05` 表示浮亏达到 5% 触发）

涨跌停幅度由 `utils.code.price_limit_pct(code, current_date, list_date)` 按板块返回（主板 10%、科创板/创业板 20%、北交所 30%、ETF 10%；新股首 5 交易日 100%）。如需扩展（如 ST ±5%），改该函数即可，`TradeEngine` 调用方不变。

---

## 6. 文件清单速查

| 文件 | 类型 | 说明 |
|------|------|------|
| `run_backtest.py` | 入口 | CLI 命令行入口（含 `--preset` 模式与 GCS 归档开关） |
| `config/backtest.yaml` | 配置 | 回测参数、费率与 GCS 产物归档配置 |
| `config/secrets.yaml` | 配置 | BigQuery / MaxCompute 凭据，**不入 git** |
| `config/secrets.yaml.example` | 配置 | secrets.yaml 模板 |
| `data_transfer/` | 工具 | 原始数据到 GCS、Parquet 构建与上传工具；当前 Parquet 目标前缀为 `gs://data-aquarium/a-share/standardized_parquet/`，并支持新增 raw GCS prefix 同步到 VM local SSD 后标准化 |
| `gcs_to_bigquery/pipeline.py` | 工具 | GCS Parquet 到 BigQuery ODS external table 的管道；支持 ADC/gcloud token 认证、持久 manifest、ODS external table 创建与 audit，并保留历史 staging/load 兼容命令 |
| `gcs_to_bigquery/config.yaml` | 配置 | GCS-to-BigQuery ODS 配置，定义 project、bucket、单 dataset、ODS external table、历史字段映射和表配置 |
| `bigquery_pipeline/` | 工具 | BigQuery 内部 DWD/DWS/ADS 加工目录；包含完整 DWD transform/audit、策略 DWS/ADS、BigQuery ML baseline、财务指标 DWD 修复、利润表/资产负债表 core 抽取、估值/基本面特征、事件/资金流特征和独立 CLI |
| `bigquery_pipeline/config.yaml` | 配置 | BigQuery 内部加工配置，定义 project、bucket、dataset、当前正式 GCS prefix、财务/基本面读取参数、ADS 候选数量和 BQML 训练/预测窗口 |
| `scripts/legacy/` | 工具 | 历史 GCE VM 恢复脚本归档，包含硬编码 `/mnt/localssd/...` 路径，不属于新装载流程 |
| `data_layer/base_data_source.py` | 抽象 | 数据源接口 |
| `data_layer/bigquery_source.py` | 实现 | Google Cloud BigQuery 数据源（默认） |
| `data_layer/maxcompute_source.py` | 实现 | 阿里云 MaxCompute 数据源（保留备选） |
| `data_layer/akshare_source.py` | 实现 | AKShare 数据源（保留备选） |
| `data_layer/tushare_source.py` | 实现 | Tushare 数据源（保留备选） |
| `data_layer/local_storage.py` | 工具 | 本地缓存读写 |
| `account/portfolio.py` | 核心 | 虚拟账户 |
| `account/position.py` | 核心 | 单股持仓（T+1） |
| `engine/backtest.py` | 核心 | 回测引擎 |
| `engine/trade_engine.py` | 核心 | 撮合引擎（MARKET/LIMIT/STOP） |
| `engine/paper_trader.py` | 核心 | 虚拟盘（已在 engine.__init__ 导出） |
| `strategy/base_strategy.py` | 抽象 | 策略基类与 Context |
| `strategy/double_ma/` | 实验包 | 双均线策略（strategy.py + config.yaml + README.md + runs/） |
| `strategy/ml_stock_picker/` | 实验包 | 机器学习选股策略；训练脚本支持 BigQuery DWS 增强特征，回测调仓日优先读取 DWS 快照，模型缺失时使用确定性 fallback score |
| `strategy/bqml_signal_picker/` | 实验包 | BigQuery ML ADS 信号真实撮合策略；动态读取 ADS 候选池，默认 10 万资金、最多 5 只持仓、5 个交易日持有期 |
| `strategy/momentum.py` | 单文件 | 月度动量策略示例 |
| `strategy/multi_factor.py` | 单文件 | 多因子选股策略示例 |
| `strategy/intraday_ma.py` | 单文件 | 日内双均线策略示例 |
| `strategy/ml_stock_picker/` | 实验包 | 单模型 LightGBM 选股（5 日固定调仓 + Top-K，无止损/regime）|
| `strategy/ml_multi_horizon_picker/` | 实验包 | 多 Horizon ML 策略（PRD_20260524_12/13/14/15/16、PRD_20260525_01）：4 buy 模型 × horizon{1,5,10,20} + 1 sell 风险模型 + 多卖出触发；regime 由沪深300趋势/动量/回撤/波动 + 股票池市场广度共同判定，并直接约束风险预算，默认 bull 最多 5 只/85% 资金、neutral 最多 3 只/45% 资金、bear 0 只/0% 资金且清掉可卖持仓；支持走步重训（walk_forward.py / model_registry.py）、可交易过滤（tradable.py，schema 与 BigQueryDataSource.trading_permissions 对齐）、**Cloud Run 并行训练**（build_registry.py + deploy/cloud_run_walk_forward/），并可在回测 preset 中直接读取 `gs://data-aquarium/models/walk_forward/registry.json`；模型按 `train_end_date < current_date` 生效，月末训练模型从下一交易日开始使用 |
| `strategy/ml_rich_picker/` | 实验包 | **富特征 ML 策略**（PRD_20260525_03）：继承 ml_multi_horizon，特征从 17 维扩展到 30 维（+ 8 维基本面 PE/PB/ROE/毛利率 + 5 维资金流 龙虎榜/主力净流入/涨停连板/开盘啦），sell 输入 39 维；BigQuery JOIN 在 SQL 层完成，LEFT JOIN 缺失喂 NaN 给 LightGBM 原生处理；initialize 预拉全历史可交易宽表 + (date, code) 索引，handle_data 按当日流动性动态 universe 查表；交易 score 固定使用 `decision_horizon=5`，正式模型包只强制 `buy_h5 + sell_v1`；成交/NAV 用不复权价格，持仓状态特征用 qfq rich 价格对齐训练；模型存储 `gs://data-aquarium/models/walk_forward_rich/` |
| `deploy/cloud_run_walk_forward/` | 部署 | Cloud Run Job 并行走步训练（PRD_20260524_14）：Dockerfile + run.sh + walk_forward_cloud_config.yaml；8 并发 ~15 分钟跑完 5 年走步训练，~HK$1 |
| `deploy/cloud_run_walk_forward_rich/` | 部署 | Cloud Run Job 富特征走步训练（PRD_20260525_03）：复用 walk_forward 部署套路，独立镜像 `ml-rich-picker`、独立模型 GCS 路径 |
| `deploy/cloud_run_backtest/` | 部署 | Cloud Run Job 完整回测（PRD_20260524_16）：Dockerfile + cloudbuild.yaml + run.sh；复用 GCS walk-forward registry，在 GCP 上运行 2020-01-02 至 2026-04-30 月度重训真实撮合回测，并归档产物到 GCS |
| `analytics/metrics.py` | 工具 | 绩效指标（含 FIFO 配对胜率/盈亏比） |
| `analytics/plotter.py` | 工具 | 可视化（自动探测中文字体） |
| `analytics/report.py` | 工具 | HTML 报告（对齐 summary.md） |
| `analytics/summary.py` | 工具 | Markdown 报告 |
| `analytics/gcs_archive.py` | 工具 | 回测输出目录上传到 GCS，并生成归档 manifest |
| `analytics/daily_diagnostics.py` | 工具 | 逐日诊断 CSV 输出（两行表头：中文 + 英文） |
| `scripts/sync_cloud_run_daily_logs.py` | 工具 | 从 Cloud Run `DAY_*` 日志同步本地 live CSV 到 `/Users/luna/Desktop/output/<run>/`，用于长回测实时观察 |
| `utils/calendar.py` | 工具 | A 股交易日历（chinese_calendar 接入） |
| `utils/code.py` | 工具 | 股票代码归一化与双向映射 |
| `utils/logger.py` | 工具 | 日志配置 |
| `analytics/cost/` | 工具 | GCP 账单导出 BigQuery 成本分析（PRD_20260524_04）；CLI `python -m analytics.cost`；SQL 模板独立、仅查询 `gcp_billing` dataset，**不进 BigQueryDataSource 路径** |
| `config/gcp_billing.yaml` | 配置 | 账单 dataset / 表名模板 / 默认查询参数；`billing_account_id` 可用环境变量 `GCP_BILLING_ACCOUNT_ID` 覆盖 |
| `tests/` | 测试 | 单元用例（含 `test_cost_queries.py` 10 用例） |
| `requirements.txt` | 依赖 | 运行依赖 |
| `requirements-dev.txt` | 依赖 | 开发依赖（pytest） |
| `pytest.ini` | 配置 | pytest 配置 |
| `.gitmessage.txt` | 开发工具 | Git commit template，提示中文提交规范和 Agent 归因环境变量 |
| `.githooks/commit-msg` / `.githooks/prepare-commit-msg` | 开发工具 | 版本化 Git hook，自动补充 `Agent` / `Agent-Model` / `Agent-Task` trailer；本地需配置 `core.hooksPath=.githooks` |

---

## 7. 运维与成本监控

GCP 账单数据通过 Google 官方 **Cloud Billing Export to BigQuery** 流入独立 dataset，与业务数据严格分层：

| Dataset | 区域 | 用途 | 谁可以读 |
|---|---|---|---|
| `ashare` | `asia-east2` | ODS / DWD / DWS / ADS 业务数据 | `data_layer.BigQueryDataSource` + `bigquery_pipeline` |
| `gcp_billing` | `asia-east2` | 账单 standard / detailed / pricing 三张导出表 | 仅 `analytics/cost/` CLI 与 ad-hoc Notebook |

设计原则：

- **运维数据与业务数据隔离**：`BigQueryDataSource` 不读 `gcp_billing`；账单分析不写入业务 dataset
- **跨区域成本规避**：`gcp_billing` dataset 必须与 `ashare` 同区域（`asia-east2`），否则跨区查询触发出口网络费且 BQ 不允许跨区 JOIN
- **净成本计算口径**：所有查询模板使用 `SUM(cost) + SUM(UNNEST(credits).amount)`，绝不裸用 `cost`
- **必须分区过滤**：所有 `gcp_billing` 查询必须带 `WHERE DATE(_PARTITIONTIME) >= ...`，避免全表扫描

详见 [`analytics/cost/README.md`](analytics/cost/README.md) 与 PRD_20260524_04。

---

*本文档随代码迭代更新，最新版本以仓库内文件为准。*
