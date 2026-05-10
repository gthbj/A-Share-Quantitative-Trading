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
│  BaseDataSource → AKShareDataSource        │
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
| `base_data_source.py` | 定义 `BaseDataSource` 抽象基类与 `Bar` 数据模型。所有数据源（AKShare、Tushare、本地文件）必须实现此接口，保证上层无感知。 |
| `akshare_source.py` | AKShare 免费数据源实现。首次请求调用 API 拉取并写入 `LocalStorage`；后续优先读本地缓存，支持增量更新。 |
| `local_storage.py` | 本地数据缓存管理器。支持 Parquet/CSV 格式，按 `data/raw/daily/{code}.parquet` 组织，提供按日期范围快速索引。 |

**设计要点**：
- 抽象接口隔离具体数据源，便于后续接入 Wind、Tushare Pro 等付费源。
- 缓存策略为 **写时缓存**：首次从远程拉取后自动落盘，不预加载全量数据。

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
| `backtest.py` | **回测主引擎**。按交易日历逐日推进，调用策略生命周期（`before_trading_start` → `handle_data` → `after_trading_end`），预加载行情数据，收集订单并交由 `TradeEngine` 撮合，最后记录每日 NAV。 |
| `paper_trader.py` | **虚拟盘**。状态持久化到 `data/paper_state.json`，支持断点续跑。每日收盘后读取最新行情，更新持仓市值，可扩展为定时自动运行。 |

**设计要点**：
- `TradeEngine` 与 `BacktestEngine` 分离：前者只负责"一笔订单能否成交"，后者负责"何时调用策略、如何组织交易日历"。
- 回测默认采用 **T+1 开盘价成交**（`price_type="next_open"`），避免未来函数（Lookahead Bias）。
- 涨跌停判定基于 `prev_close` 与当日 `open` 计算，主板简化为 ±10%（实际可通过配置扩展科创板 ±20%、ST ±5%）。

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

### 4.5 为什么不支持分钟级回测？

当前版本为 **M1（基础框架）** 交付物，优先保证日线回测的完整性与正确性。代码结构已预留分钟级扩展：
- `BaseDataSource.get_daily_bars` 可扩展为 `get_bars(period="1min")`。
- `BacktestEngine` 的逐日循环可改写为逐 Bar 循环。

分钟级回测将在 **M4（高级功能）** 阶段实现。

---

## 5. 扩展指南

### 5.1 接入新数据源

1. 继承 `BaseDataSource`，实现 `get_daily_bars`、`get_stock_list`、`get_index_constituents`。
2. 在 `run_backtest.py` 中替换 `AKShareDataSource()` 为你的实现。

### 5.2 添加新策略

1. 在 `strategy/` 下新建 `.py` 文件。
2. 继承 `BaseStrategy`，重写 `initialize` 和 `handle_data`。
3. 运行：`python run_backtest.py --strategy strategy.your_module.YourStrategy`

### 5.3 修改交易规则

编辑 `config/backtest.yaml`：
- `trading.commission_rate`：佣金费率
- `slippage.value`：滑点大小
- `execution.price_type`：`next_open` 或 `current_close`

如需修改涨跌停幅度（如科创板 20%），编辑 `engine/trade_engine.py` 的 `_try_fill` 方法。

---

## 6. 文件清单速查

| 文件 | 类型 | 说明 |
|------|------|------|
| `run_backtest.py` | 入口 | CLI 命令行入口 |
| `config/backtest.yaml` | 配置 | 回测参数与费率 |
| `data_layer/base_data_source.py` | 抽象 | 数据源接口 |
| `data_layer/akshare_source.py` | 实现 | AKShare 数据源 |
| `data_layer/local_storage.py` | 工具 | 本地缓存读写 |
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
