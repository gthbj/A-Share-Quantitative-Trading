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
│  BaseDataSource → MaxComputeDataSource     │
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
| `maxcompute_source.py` | **当前默认数据源**。通过 pyodps 连接阿里云 MaxCompute（项目 `a_share_historical_data`，北京区 endpoint）拉取 A 股历史行情。包含本地 Parquet 缓存与缓存清理策略（按保留天数 + 总容量上限）。接入进度：✅ 5min K 线（`cn_stock_kline_5min` 表）；✅ 15min ETF K 线（`cn_etf_kline_15min` 表，含 510300.SH）；✅ 股票列表（从 5min 表 DISTINCT 派生）；⚠️ 复权接口预留（待因子表接入）；⚠️ 指数成分股暂返回空列表；❌ daily / 1/30/60min 周期及普通股票 15min 表尚未建立。详见 §4.7 / §4.8 / §4.9。 |
| `akshare_source.py` | AKShare 免费数据源实现（已保留为备选，但未被 `run_backtest.py` 装载）。首次请求调用 API 拉取并写入 `LocalStorage`；后续优先读本地缓存，支持增量更新。 |
| `local_storage.py` | 本地数据缓存管理器。支持 Parquet/CSV 格式，按 `data/raw/daily/{code}_{period}.parquet` 组织（如 `000001_1min.parquet`），避免不同周期数据互相覆盖，提供按日期范围快速索引。 |

**设计要点**：
- 抽象接口隔离具体数据源，便于后续接入 Wind、Tushare Pro 等付费源。
- 缓存策略为 **写时缓存**：首次从远程拉取后自动落盘，不预加载全量数据。
- **缓存清理（MaxCompute 数据源专有）**：实例化时根据 `data.cache.retention_days` 与 `data.cache.max_size_gb` 自动清理过期或溢出的本地缓存文件，避免长期累积。
- 分钟级数据时间列统一格式化为 `YYYYMMDDHHMM`（12位），便于按交易日前缀快速筛选。
- **凭据隔离**：MaxCompute AccessKey 通过 `config/secrets.yaml`（已加入 `.gitignore`）或环境变量 `MAXCOMPUTE_ACCESS_ID` / `MAXCOMPUTE_ACCESS_KEY` 注入，不入仓库。

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
| `trade_engine.py` | **交易撮合引擎**。职责：① 验证订单合法性（资金、T+1、涨跌停、成交量限制）；② 计算成交价（支持开盘价/收盘价模式，叠加滑点）；③ 计算并扣除交易费用（佣金、印花税、过户费）；④ 调用 Portfolio 更新持仓。 |
| `backtest.py` | **回测主引擎**。支持日线/分钟线双频回测。按交易日历逐日（daily）或逐 Bar（1min/5min/15min/30min/60min）推进，调用策略生命周期，收集订单并交由 `TradeEngine` 撮合，记录 NAV。分钟级回测中 `before_trading_start` / `after_trading_end` 仍按交易日边界调用。内置**全局止损模块**：策略 `handle_data` 执行完毕后，自动扫描持仓，当浮亏超过阈值时生成 MARKET 卖出单，与策略订单一并交由 `TradeEngine` 执行。止损对策略完全透明，无需修改任何策略代码。 |
| `paper_trader.py` | **虚拟盘**。状态持久化到 `data/paper_state.json`，支持断点续跑。每日收盘后读取最新行情，更新持仓市值，可扩展为定时自动运行。 |

**设计要点**：
- `TradeEngine` 与 `BacktestEngine` 分离：前者只负责"一笔订单能否成交"，后者负责"何时调用策略、如何组织交易日历"。
- 回测默认采用 **T+1 开盘价成交**（`price_type="next_open"`），避免未来函数（Lookahead Bias）。
- 涨跌停判定基于 `prev_close` 与当日 `open` 计算，主板简化为 ±10%（实际可通过配置扩展科创板 ±20%、ST ±5%）。
- 分钟级回测保持 T+1 以**交易日**为维度：当日买入的股票在当日剩余所有分钟内均不可卖，下一交易日开盘后解冻。
- **全局止损**：引擎每日/每 Bar 在策略信号之后、撮合之前自动检查持仓浮亏。仅对 `sellable_qty > 0` 的仓位生效（T+1 当日买入不会被止），止损单以 `OrderType.MARKET` 发出，与策略订单一起进入 `TradeEngine` 按 A 股规则撮合。 |

---

### 2.4 strategy/ — 策略层

| 文件 | 职责 |
|------|------|
| `base_strategy.py` | 策略抽象基类。定义生命周期钩子（`initialize` / `before_trading_start` / `handle_data` / `after_trading_end`）。`Context` 对象封装下单接口（`order`）与数据查询（`get_price`）。 |
| `double_ma.py` | 双均线策略示例：MA5 金叉买入、死叉卖出。 |
| `momentum.py` | 月度动量策略示例：每月初买入上月涨幅前 N 名，等权持有。 |
| `multi_factor.py` | 多因子策略示例：基于 PE/PB/ROE 综合评分选股（演示框架，实际需接入财务数据库）。 |

**设计要点**：
- 策略与引擎完全解耦：策略只知道 `Context` 接口，不感知回测循环细节。
- `order_target_percent` / `order_target_value` 目前为预留接口，建议用户手动计算目标数量后调用 `order()`，避免框架隐式行为导致不可预期。

---

### 2.5 analytics/ — 绩效分析层

| 文件 | 职责 |
|------|------|
| `metrics.py` | 绩效指标计算。包括累计/年化收益率、最大回撤、波动率、夏普比率、索提诺比率、Beta、Alpha、信息比率等。 |
| `plotter.py` | 可视化绘图。使用 Matplotlib 生成累计收益对比图、回撤曲线、月度收益热力图。 |
| `report.py` | HTML 报告生成器。基于模板引擎渲染静态报告，内含图表嵌入，便于分享。 |

---

### 2.6 utils/ — 工具层

| 文件 | 职责 |
|------|------|
| `calendar.py` | A 股交易日历。提供 `is_trading_day`、`get_trading_days`、`next_trading_day` 等方法。当前为简化实现（硬编码 2020-2024 长假），后续可接入 `exchange_calendars` 或 AKShare 精确日历。 |
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

### 4.6 为什么默认数据源切换为 MaxCompute？

历史上本框架默认使用 AKShare（免费）+ Tushare Pro（备选）。但 AKShare 接口稳定性差、有 IP 限流且分钟级数据被部分屏蔽；Tushare 的高频接口需要积分门槛。项目所有者已在阿里云 MaxCompute 中维护了 `a_share_historical_data` 项目作为权威历史行情源，因此将默认数据源切换为 MaxCompute。

**实现要点**：
- `MaxComputeDataSource` 实现 `BaseDataSource` 全部三个抽象方法。
- 连接懒加载，凭据通过 `config/secrets.yaml`（gitignored）或环境变量注入。
- 本地 Parquet 缓存与清理策略由 `data.cache.retention_days` / `data.cache.max_size_gb` 控制，避免 SQL 重复计费。
- `akshare_source.py` 与 `tushare_source.py` 保留但不再被 `run_backtest.py` 装载，以便后续按需切换。

### 4.7 MaxCompute 表结构与接入进度

> **当前状态**（截至 PRD_20260519_03 完成）：

| 用途 | 配置键（`config/backtest.yaml`） | 状态 | 备注 |
|------|----------------------------------|------|------|
| 5min K 线 | `data.maxcompute.tables.kline_5min` | ✅ **已接入** | 表 `cn_stock_kline_5min`，详见 §4.8 |
| ETF 15min K 线 | `data.maxcompute.tables.kline_etf_15min` | ✅ **已接入** | 表 `cn_etf_kline_15min`，详见 §4.9 |
| 复权因子 | `data.maxcompute.tables.adjust_factor` | 🟡 接口预留 | `_apply_adjust()` 占位，待复权 PRD 接入 |
| 股票列表 | （无需配置） | ✅ 已接入（派生） | 由 5min 表 `SELECT DISTINCT code, name` 派生；`list_date / industry` 为空 |
| 指数成分股 | `data.maxcompute.tables.index_constituent` | 🟡 暂返回空 | 调用 `get_index_constituents()` 返回 `[]` + WARNING，不阻塞策略 |
| 日K | `data.maxcompute.tables.daily` | ❌ 待建表 | 调用 `get_bars(period="daily")` 抛 `NotImplementedError` |
| 1min K | `data.maxcompute.tables.kline_1min` | ❌ 待建表 | 同上 |
| 普通股票 15min K | `data.maxcompute.tables.kline_15min` | ❌ 待建表 | ETF 15min 已有专表；普通股票 15min 待建 |
| 30/60min K | `data.maxcompute.tables.kline_30/60min` | ❌ 待建表 | 调用时抛 `NotImplementedError` |
| 专用 stock_info | `data.maxcompute.tables.stock_info` | ❌ 待建表 | 若建立则替代 5min 派生路径；当前未启用 |

后续每一项接入时，对应方法 `_fetch_daily_bars` / `_fetch_minute_bars` 内分支需按实际字段名拼接 SQL，列名映射到框架标准列 `[code, date, open, high, low, close, volume, amount]`。

### 4.8 5min 表 (`cn_stock_kline_5min`) 接入细节

**表结构**（PRD_20260519_02 调研结果）：

| 列 | 类型 | 含义 |
|---|---|---|
| `trade_time` | DATETIME | K 线时间，精确到分钟 |
| `code` | STRING | 股票代码，**表内格式为 `shXXXXXX` / `szXXXXXX`**（小写前缀） |
| `name` | STRING | 股票中文名 |
| `open / close / high / low` | DOUBLE | OHLC |
| `volume` | BIGINT | 成交量（股） |
| `amount` | DOUBLE | 成交额（元） |
| `change_pct / amplitude` | DOUBLE | 涨跌幅 / 振幅（框架不返回） |
| `year_month` | STRING | **分区字段**，格式 `YYYYMM` |

**关键约定**：

1. **代码格式双向映射**：上层调用始终用框架格式 `XXXXXX.SH` / `XXXXXX.SZ`；`_to_exchange_code()` 在拼 SQL 前转为表格式 `shXXXXXX` / `szXXXXXX`；`_to_framework_code()` 在 DataFrame 返回前转回框架格式。**对策略层完全透明。**
2. **分区裁剪**：`get_bars()` 根据请求的 `[start_date, end_date]` 计算覆盖的 `year_month` 列表（`_year_months_in_range()`），SQL 中以 `year_month IN ('200006', '200007', ...)` 触发分区裁剪。**不裁剪则全表扫描，计费成本约 1000 倍。**
3. **时间格式化**：SQL 直接 `SELECT trade_time`（DATETIME），由 Python 侧 `pd.to_datetime(...).strftime("%Y%m%d%H%M")` 转为 12 位 `YYYYMMDDHHMM` 字符串，与框架分钟级 `date` 列约定一致。避免对 MaxCompute SQL 函数版本差异的依赖。
4. **复权当前不支持**：5min 表为不复权原始价。`adjust ∈ {qfq, hfq}` 时 `_apply_adjust()` 仅打 WARNING 日志，**返回原始价**。待复权因子表接入后扩展该方法。
5. **数据时间覆盖**：当前 5min 表仅含 `200006 ~ 200212` 共 31 个分区（约 2.5 年）。超出范围的请求返回空 DataFrame，不报错。数据扩充由独立 PRD 负责。

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

---

## 5. 扩展指南

### 5.1 接入新数据源

1. 继承 `BaseDataSource`，实现 `get_bars`、`get_stock_list`、`get_index_constituents`。
2. 在 `run_backtest.py` 中替换 `build_maxcompute_data_source()` 的调用为你的实现，或在 `data.source` 配置项基础上扩展分支。

### 5.2 添加新策略

1. 在 `strategy/` 下新建 `.py` 文件。
2. 继承 `BaseStrategy`，重写 `initialize` 和 `handle_data`。
3. 运行：`python run_backtest.py --strategy strategy.your_module.YourStrategy`

### 5.3 修改交易规则

编辑 `config/backtest.yaml`：
- `trading.commission_rate`：佣金费率
- `slippage.value`：滑点大小
- `execution.price_type`：`next_open` 或 `current_close`
- `stop_loss.enabled`：是否启用全局止损
- `stop_loss.threshold`：止损阈值（如 `0.05` 表示浮亏达到 5% 触发）

如需修改涨跌停幅度（如科创板 20%），编辑 `engine/trade_engine.py` 的 `_try_fill` 方法。

---

## 6. 文件清单速查

| 文件 | 类型 | 说明 |
|------|------|------|
| `run_backtest.py` | 入口 | CLI 命令行入口 |
| `config/backtest.yaml` | 配置 | 回测参数与费率 |
| `data_layer/base_data_source.py` | 抽象 | 数据源接口 |
| `data_layer/maxcompute_source.py` | 实现 | 阿里云 MaxCompute 数据源（默认） |
| `data_layer/akshare_source.py` | 实现 | AKShare 数据源（保留备选） |
| `data_layer/local_storage.py` | 工具 | 本地缓存读写 |
| `config/secrets.yaml` | 配置 | MaxCompute 凭据，**不入 git** |
| `config/secrets.yaml.example` | 配置 | secrets.yaml 模板 |
| `account/portfolio.py` | 核心 | 虚拟账户 |
| `account/position.py` | 核心 | 单股持仓（T+1） |
| `engine/backtest.py` | 核心 | 回测引擎 |
| `engine/trade_engine.py` | 核心 | 撮合引擎 |
| `engine/paper_trader.py` | 核心 | 虚拟盘 |
| `strategy/base_strategy.py` | 抽象 | 策略基类与 Context |
| `analytics/metrics.py` | 工具 | 绩效指标 |
| `analytics/plotter.py` | 工具 | 可视化 |
| `analytics/report.py` | 工具 | HTML 报告 |
| `utils/calendar.py` | 工具 | 交易日历 |
| `utils/logger.py` | 工具 | 日志配置 |

---

*本文档随代码迭代更新，最新版本以仓库内文件为准。*
