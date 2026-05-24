# A-Share Quantitative Trading — 已知问题与待优化清单

> **维护原则**：此文档记录已发现、但尚未修复的问题。每项需包含：现象描述、根因分析、建议修复方向、优先级。

---

## ✅ 已修复（2026-05-20，PRD_20260520_01 ~ 11）

| 编号 | 问题 | 解决方案 |
|------|------|---------|
| P0-1 | 收益曲线/回撤图水平线段 | 此前已在 PRD_20260519_03 之前修复（按日期长度分别解析 YYYYMMDD / YYYYMMDDHHMM） |
| P0-2 | 止损单未来函数 | 此前已在 PRD_20260510_02 中改为次日开盘成交 |
| P1-1 | AKShare 分钟数据不可达 | 默认切换到 MaxCompute（PRD_20260519_01），后迁移至 BigQuery（PRD_20260522_01/02） |
| P1-2 | Matplotlib 中文字体 | PRD_20260520_05：`Plotter` 模块加载时自动探测系统中文字体 |
| P2-1 | 交易日历硬编码 | PRD_20260520_05：接入 `chinese_calendar`，硬编码保留为 fallback |
| P2-2 | 缺少单元测试 | PRD_20260520_07：tests/ 共 64 个用例，覆盖 TradeEngine / Position / Portfolio / metrics / code |
| —    | metrics.fills 永远收不到 fills | PRD_20260520_02：FIFO 配对实现 win_rate / pl_ratio |
| —    | get_price fallback 分钟级日期估算过宽 | PRD_20260520_02：按 frequency 调整 days_back 估算 |
| —    | Order qty < 100 静默截断 | PRD_20260520_02：截断时 WARNING |
| —    | order_target_percent 静默不做事 | PRD_20260520_02：改为抛 NotImplementedError |
| —    | 代码归一化两份维护 | PRD_20260520_03：提取到 `utils/code.py`，CLI / data_layer / engine / strategy 共享 |
| —    | 基准用回测频率拉数据导致 metrics 失真 | PRD_20260520_03：基准优先 daily，失败降级到 frequency |
| —    | HTML 报告内容过简 | PRD_20260520_04：对齐 summary.md 全部 9 节内容（chip + 折叠明细表） |
| —    | engine/__init__ 未导出 PaperTrader | PRD_20260520_01：已导出 |
| —    | README/ARCHITECTURE 与现状不一致 | PRD_20260520_01：全文同步 |
| —    | TradeEngine 仅支持 MARKET 单 | PRD_20260520_06：实现 LIMIT/STOP 撮合 + Context.limit_order / stop_order |
| P2-3 | 涨跌停固定 ±10%（未区分科创板/创业板/ETF） | PRD_20260520_08：抽 `utils.code.price_limit_pct(code, date)`；主板 10% / 科创板创业板 20% / ETF 10%；创业板按 2020-08-24 切换 |
| P2-4 | paper_trader 缺少策略信号驱动 | PRD_20260520_09：`run_once` 完整接入策略循环（预加载历史 → next_open 撮合 → handle_data → 止损检查 → 状态持久化）；state.json 扩展存储策略类与 kwargs、user_data、待执行订单、待止损队列 |
| TBD-2 | 限价单/止损单无过期与取消机制（挂单当根 bar 不成交即消失） | PRD_20260520_10：`TradeEngine.pending_orders` 挂单池；`sweep_pending` 每根 bar 扫描；`time_in_force` DAY/GTC + `expire_date`；`Context.cancel_order`；`BacktestEngine` 两条循环路径均已集成；新增 21 个单元测试（AC-9.1~9.7）|
| TBD-4 | 北交所代码 / 涨跌停 ±30% 未支持 | PRD_20260520_10：`_BJ_PREFIXES` + `normalize_code` / `to_exchange_code` / `to_framework_code` 全支持 `.BJ`；`price_limit_pct` 北交所分支返回 0.30 |
| TBD-5 | 新股上市首日涨跌幅未支持 | PRD_20260520_10：`price_limit_pct` 加 `list_date` 参数；`_is_within_first_n_trading_days` 用 TradingCalendar 判定；首 5 交易日返回 1.0；`Order.list_date` 字段 + `TradeEngine.listing_dates` / `set_listing_dates` 双通道注入 |
| TBD-5（缓存） | MaxCompute 缓存命中路径 `load_bars` end_date 裁剪丢分钟数据 | PRD_20260520_11：`LocalStorage.load_bars()` 过滤改为 `date <= end_date + "9999"`，对齐非缓存路径行为 |

---

## 🟢 待优化（建议优先级）

### TBD-1: chinese_calendar 覆盖范围有限

**现象**  
`chinese_calendar` 当前版本（1.11.0）覆盖到 2026 年。2027+ 会降级到"非周末"启发式，导致 2027 假期判定错误。

**修复方向**  
- 每年初 `pip install -U chinese-calendar`
- 或接入 `exchange_calendars` 库的 XSHG 日历（更长期覆盖）

---

### TBD-3: ST/*ST 股票涨跌停 ±5% 未支持

**现象**  
PRD_20260520_08 实现了板块细分（主板 10%、科创板/创业板 20%、ETF 10%），但 ST/*ST 股票应为 ±5%，目前一律按板块默认值放行。

**根因分析**  
当前数据源（BigQuery `fact_equity_kline_1d`）已包含 ST 状态表 `fact_st_status_1d`，但引擎尚未接入。

**修复方向**  
- 等股票列表 / 基本面表上线后接入 ST 状态（按日期生效）
- 或给 `Order` 加 `is_st: bool` 字段由策略层显式标注
- 修改点集中在 `utils.code.price_limit_pct`，调用方不变

---

### TBD-4: 缺少行业 / 财务因子数据

**现象**  
`MultiFactorStrategy` 仅用动量作为 PE/PB/ROE 的代理；指数成分股表 `index_constituent` 未配置；股票列表派生自 5min 表，`list_date / industry` 为空。

**修复方向**  
- BigQuery 已上线财务表、行业表、指数成分股表（`ashare` dataset 内 DWD/DWS 层），待引擎接入
- `MultiFactorStrategy` 接入真实因子数据

---

---
### TBD-6: 分钟级缓存覆盖范围检查失效（P0）

**现象**  
`BigQueryDataSource.get_bars()` 继承了与 MaxCompute 相同的缓存命中判断逻辑：字符串比较 `cmin <= start_date`。分钟级 `date` 为 12 位（`YYYYMMDDHHMM`），而 `start_date` 为 8 位（`YYYYMMDD`），字典序下 `"200006090000" <= "20000609"` 为 `False`，导致**分钟级缓存永远不会被命中**，重复查询 BigQuery 产生额外费用。

**根因分析**  
`get_bars()` 中未对分钟级与日线级的日期长度做统一处理，直接进行字符串比较。

**修复方向**  
- 统一比较长度：将 `start_date` / `end_date` 扩展为与缓存数据相同的位数后再比较；或统一截取日期前缀。
- 位置：`data_layer/bigquery_source.py` `get_bars()` 方法（与 `maxcompute_source.py` 同源逻辑）。

---

### TBD-7: 回测引擎中 LIMIT/STOP 买单资金重复冻结（P0）

**现象**  
`BacktestEngine` 在调用 `execute_orders` 前对 LIMIT/STOP 买单预执行 `reserve_cash`；若当根 bar 未成交，订单被加入 `pending_orders`。下一根 bar `sweep_pending` 会**再次** `reserve_cash`，导致同一笔订单资金被双重冻结。多扣的 `frozen_cash` 要等到次日 `before_trading` 才释放，分钟级回测中尤为严重。

**根因分析**  
`execute_orders` 未成交入池前未释放预留资金，而 `sweep_pending` 自行管理资金预留，两者职责重叠。

**修复方向**  
- 方案 A：`execute_orders` 未成交的 LIMIT/STOP 买单在 `add_pending` 前调用 `release_cash`，完全交由 `sweep_pending` 管理。
- 方案 B：取消 `BacktestEngine` 对 LIMIT/STOP 订单的预冻结，仅对 MARKET 单预冻结。

---

### TBD-8: MARKET 单未成交时资金当日不释放（P1）

**现象**  
MARKET 单因涨停/跌停等原因未成交后会被直接丢弃（不入 `pending_orders`），但 `BacktestEngine` 中已 `reserve_cash` 的资金未被释放。日线回测中次日 `before_trading` 会释放，影响较小；**分钟级回测中该笔资金在当日剩余时间一直处于冻结状态**。

**根因分析**  
`BacktestEngine` 对 MARKET 单预冻结后，未在 `execute_orders` 返回后检查未成交订单并释放资金。

**修复方向**  
- `execute_orders` 返回后，对未成交且未入池的 MARKET 买单统一 `release_cash`。
- 或重构资金管理：所有未成交买单统一在 bar 结束后释放冻结资金，成交单在 `apply_buy_fill` 中处理差额。

---

### TBD-9: Alpha 计算使用非年化基准收益（P1）

**现象**  
`analytics/metrics.py` 中 Jensen Alpha 公式使用了 `result.benchmark_return`，但 `benchmark_return` 是区间**总收益**（`bench.iloc[-1] / bench.iloc[0] - 1`），而非年化收益。长周期回测下总收益与年化收益差距显著，Alpha 严重失真。

**根因分析**  
公式代入时未对基准收益做年化转换。

**修复方向**  
- 将 `benchmark_return` 按回测时长转换为年化收益后代入 Alpha 公式；或在 `MetricsResult` 中新增 `benchmark_annual_return` 字段。

---

### TBD-10: turnover 换手率指标未计算（P1）

**现象**  
`MetricsResult` 中声明了 `turnover: float = 0.0`，但 `calculate_metrics()` 中**完全没有计算逻辑**，该字段恒为 0。

**修复方向**  
- 按标准定义计算：`(期间买入金额 + 期间卖出金额) / 2 / 平均资产` 或 `期间成交金额 / 平均资产`。
- 需要 `fills` 数据与 `nav` 序列配合计算。

---

### TBD-11: 回测主循环存在 O(n) 性能瓶颈（P1）

**现象**  
`_run_daily` 与 `_run_intraday` 中，每根 bar 都用 `df[df["date"] == date_str]` 进行全表扫描匹配。当 universe 较大（如全 A 股）或历史数据较长时，回测速度显著下降。

**根因分析**  
DataFrame 布尔索引为 O(n) 操作，且在每个交易日/每根 bar、每只股票上重复执行。

**修复方向**  
- 预先将 `all_bars[code]` 的 `date` 列设为索引（`set_index`），或使用 `dict` 将 date 映射到 row 实现 O(1) 查找。
- 分钟级并集排序也可优化为基于优先队列的合并遍历。

---

### TBD-12: 数据源 SQL 注入风险与性能问题（P1）

**现象**  
1. `maxcompute_source.py` 中 `_fetch_5min_bars` / `_fetch_etf_15min_bars` 直接将 `code` 拼接到 SQL 字符串（`WHERE code = '{exchange_code}'`），存在 SQL 注入风险。`bigquery_source.py` 的 `_fetch_daily_bars` 也存在相同问题。
2. `maxcompute_source.py` 的 `get_stock_list` 执行无分区条件的 `SELECT DISTINCT code, name FROM {table}`，对大表会触发全表扫描，计费成本极高。`bigquery_source.py` 已改用 `dim_security` 小表，该问题已缓解。
3. `_execute_sql` 对所有异常（包括 SQL 语法错误、表不存在等）都执行指数退避重试，浪费资源。

**修复方向**  
- `code` 使用参数化查询或预校验格式（仅允许 `\d{6}\.(SH|SZ|BJ)` 等）。
- `get_stock_list` 优先使用独立的 `dim_security` 表（BigQuery 已接入）。
- 重试逻辑区分可重试异常（网络超时、连接断开）与不可重试异常（语法错误、权限不足）。

---

### TBD-13: Sortino Ratio 分子未扣除无风险利率（P2）

**现象**  
`metrics.py` 中 `sortino_ratio = (returns.mean() * ann_factor) / (downside.std() * np.sqrt(ann_factor))`，分子直接使用了年化收益，未减去 `risk_free_rate`。

**修复方向**  
- 分子改为 `(returns.mean() * ann_factor - risk_free_rate)`，与 Sharpe Ratio 的分子处理保持一致。

---

### TBD-14: 涨跌停 prev_close fallback 导致限制失效（P2）

**现象**  
`TradeEngine._try_fill` 中：`prev_close = bar.get("prev_close", price)`。当 bar 缺少 `prev_close`（如上市首日或数据缺失）时，fallback 到 `price` 自身，导致 `price >= up_limit` 几乎恒为 `False`，**涨跌停限制在此场景下失效**。

**修复方向**  
- 缺失 `prev_close` 时采取更保守策略：如拒绝成交并打 WARNING，或至少将 `prev_close` fallback 到 `bar.get("prev_close", bar.get("close", price))` 以增加数据来源。

---

### TBD-15: `_try_fill` 买入资金检查逻辑不精确（P2）

**现象**  
`_try_fill` 中对买单的资金检查为：`portfolio.available_cash + portfolio.frozen_cash < required`。`available + frozen` 在 `reserve_cash` 后等于原始总资产，无法反映其他订单已占用的冻结资金，防御性检查形同虚设。

**修复方向**  
- 检查逻辑改为 `portfolio.available_cash < required`，或明确扣除当前订单已预留的冻结资金。

---

### TBD-16: 异常处理与日志规范（P2）

**现象**  
1. `BacktestEngine._load_benchmark()` 等位置使用宽泛的 `except Exception as e`，会吞掉真正的 Bug（如数据格式错误、类型错误）。
2. `run_backtest.py` CLI 入口大量使用 `print()` 输出信息，与框架内部统一使用的 `get_logger` 不一致，不利于日志级别控制和日志文件收集。

**修复方向**  
- 精细化异常捕获：区分 `NotImplementedError`（降级）、`ValueError`/`KeyError`（报错）、网络异常（重试）。
- CLI 统一替换为 `logger.info()` / `logger.error()`。

---

### TBD-17: 缺少事件/钩子系统（P2）

**现象**  
当前策略生命周期仅提供 `before_trading_start` / `handle_data` / `after_trading_end`。若需接入风控告警、外部信号推送、成交后回调等，必须直接修改引擎源码，耦合度高。

**修复方向**  
- 引入轻量级事件总线或钩子机制：如 `on_order_filled(fill)`、`on_stop_loss_triggered(code, qty)`、`on_day_end(record)` 等，由策略或外部模块订阅。

---

### TBD-18: 配置缺少统一校验层（P2）

**现象**  
参数来源分散（CLI args → preset config → `config/backtest.yaml` → 硬编码），没有统一的配置校验层。非法配置（如负滑点、非法 `frequency`、越界止损阈值）可能在运行中途才暴露。

**修复方向**  
- 引入 Pydantic model 或 dataclass + `__post_init__` 做统一校验与类型转换，在回测启动前一次性校验所有参数。

---

### TBD-19: 测试覆盖存在盲区（P2）

**现象**  
现有单元测试主要覆盖 `TradeEngine`、`Position`、`Portfolio`、`metrics`、`code`、`paper_trader`。但缺少对以下模块的测试：
- `analytics/plotter.py`（图表渲染、中文字体 fallback）
- `analytics/report.py`、`summary.py`（HTML/Markdown 结构断言）
- `run_backtest.py`（CLI 参数解析、preset 加载流程）
- `data_layer/bigquery_source.py`（SQL 拼接、分区裁剪、缓存命中/未命中路径）
- `data_layer/maxcompute_source.py`（保留备选）
- `data_layer/local_storage.py`（Parquet 读写、日期边界过滤）

此外，**缺少端到端集成测试**：无法验证"完整回测一次并产出正确 NAV 曲线与报告"这一核心链路。

**修复方向**  
- 补充上述模块的单元测试；
- 增加至少一条完整回测链路的集成测试（如 DoubleMAStrategy + 510300.SH + 日线/15min，断言 NAV 不为空、报告文件生成、关键指标合理）。

---

---

### TBD-20: 多因子Alpha策略——分析师预期修正因子缺失

**现象**
价值/质量/动量/低波/流动性因子全部可由当前 `ashare` dataset 财务表和日K线构造，但**分析师预期修正因子**缺失（无一致预期/盈利预测修正数据）。

**修复方向**
- 接入外部分析师预期数据源（如 Wind、朝阳永续）
- 或爬取东方财富/同花顺分析师评级页面
- 或接入 Tushare `report_rc` / `forecast` 接口

**优先级**：中

---

### TBD-21: 事件驱动策略——解禁/增减持/北向/两融/龙虎榜数据缺失

**现象**
- 财报漂移（PEAD）可用现有 `fact_earnings_forecast` + `fact_earnings_express` 实现
- 但**解禁数据**、**增减持公告**、**融资融券明细**、**陆股通（北向资金）**、**龙虎榜/大宗交易** 均缺失

**修复方向**
- 解禁/增减持：Tushare/JQData `share_float` / `stk_holder_trade`
- 融资融券：Tushare `margin_detail`
- 北向资金：Tushare `moneyflow_hsgt`
- 龙虎榜/大宗：Tushare `top_list` / `block_trade`

**优先级**：低-中（数据需额外付费或爬取）

---

### TBD-22: 市场情绪策略——融资余额/精确筹码结构数据缺失

**现象**
- 换手异常、涨停情绪可用现有日K线 + `fact_limit_price_1d` 近似实现
- 但**融资余额变化率**（无两融数据）和**精确筹码结构**（获利盘比例、筹码峰密集度，需逐笔/更低频股东明细）缺失

**修复方向**
- 融资余额：同 TBD-21，接入 Tushare `margin_detail`
- 筹码结构：用 `fact_shareholder_count` + `fact_top10_float_shareholders` 做近似；或接入 Level-2 逐笔数据

**优先级**：低

---

### TBD-23: 可转债双低/轮动策略——缺可转债数据

**现象**
`fact_fund_kline_1d` 仅覆盖 ETF/LOF（代码 51/56/58/15/16 开头），**无可转债行情数据**（可转债代码通常为 11/12 开头）。

**修复方向**
- 接入 Tushare `cb_daily` / `bond_daily`
- 或爬取集思录可转债数据

**优先级**：中（可转债是小资金优势领域）

---

*本文档最后更新：2026-05-23（补充 TBD-20 ~ TBD-23，来源于策略数据可行性评估）*
