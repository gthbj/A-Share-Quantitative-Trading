# A-Share Quantitative Trading — 已知问题与待优化清单

> **维护原则**：此文档记录已发现、但尚未修复的问题。每项需包含：现象描述、根因分析、建议修复方向、优先级。

---

## ✅ 已修复（2026-05-20，PRD_20260520_01 ~ 08）

| 编号 | 问题 | 解决方案 |
|------|------|---------|
| P0-1 | 收益曲线/回撤图水平线段 | 此前已在 PRD_20260519_03 之前修复（按日期长度分别解析 YYYYMMDD / YYYYMMDDHHMM） |
| P0-2 | 止损单未来函数 | 此前已在 PRD_20260510_02 中改为次日开盘成交 |
| P1-1 | AKShare 分钟数据不可达 | 默认切换到 MaxCompute（PRD_20260519_01） |
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

---

## 🟢 待优化（建议优先级）

### TBD-1: paper_trader 缺少策略信号驱动

**现象**  
`PaperTrader.run_once` 仅做"持仓估值与状态更新"，没有真正调用策略 `handle_data` 生成订单。

**根因分析**  
当前实现是占位骨架，未把策略循环挂接到虚拟盘日常调度上。

**修复方向**  
- 在 `run_once` 中加载策略类、构造 `Context`，调用 `handle_data` 并把订单交给 `TradeEngine` 撮合
- 状态文件中持久化策略 `user_data`（多日间状态续接）

---

### TBD-2: 限价单 / 止损单缺少过期与取消机制

**现象**  
PRD_20260520_06 实现了 LIMIT / STOP 撮合，但订单**永不过期**：未成交的挂单会一直在 `Context._orders` 中（实际上每次 `pop_orders()` 都清空了，所以现在的实现等于"挂单当根 bar 不成交就消失"）。

**修复方向**  
- `Order` 新增 `expire_date` / `time_in_force`（GTC / DAY）
- 引擎层维护一个"未成交挂单池"，每根 bar 检查触发条件直到过期

---

### TBD-3: chinese_calendar 覆盖范围有限

**现象**  
`chinese_calendar` 当前版本（1.11.0）覆盖到 2026 年。2027+ 会降级到"非周末"启发式，导致 2027 假期判定错误。

**修复方向**  
- 每年初 `pip install -U chinese-calendar`
- 或接入 `exchange_calendars` 库的 XSHG 日历（更长期覆盖）

---

### TBD-4: ST/*ST 股票涨跌停 ±5% 未支持

**现象**  
PRD_20260520_08 实现了板块细分（主板 10%、科创板/创业板 20%、ETF 10%），但 ST/*ST 股票应为 ±5%，目前一律按板块默认值放行。

**根因分析**  
当前数据源（MaxCompute 5min K 线表）不包含 ST 标签字段；`Order` 也没有 `is_st` 字段。

**修复方向**  
- 等股票列表 / 基本面表上线后接入 ST 状态（按日期生效）
- 或给 `Order` 加 `is_st: bool` 字段由策略层显式标注
- 修改点集中在 `utils.code.price_limit_pct`，调用方不变

---

### TBD-5: 北交所代码 / 涨跌停 ±30% 未支持

**现象**  
`utils.code.normalize_code` 当前不接受北交所前缀（4/8 开头），所以 `price_limit_pct` 即使加了北交所分支也不会被触发。

**修复方向**  
- 扩展 `_SH_PREFIXES` / `_SZ_PREFIXES` 或新增 `_BJ_PREFIXES`，让框架代码支持 `.BJ` 后缀
- `price_limit_pct` 加北交所分支返回 0.30
- `data_layer` 需相应支持北交所数据源（MaxCompute 表是否覆盖待确认）

---

### TBD-6: 新股上市首日涨跌幅未支持

**现象**  
A 股新股上市首日特殊涨跌幅（主板 ±44%、创业板/科创板无限制），目前一律按板块常规规则。

**修复方向**  
- 需先接入股票 `list_date` 字段
- `price_limit_pct` 增加 `list_date` 参数；当 `current_date == list_date` 时返回特殊值
- 撮合逻辑需要区分"无限制"（创业板首日）与"特殊比例"（主板 44%）

---

### TBD-7: 缺少行业 / 财务因子数据

**现象**  
`MultiFactorStrategy` 仅用动量作为 PE/PB/ROE 的代理；指数成分股表 `index_constituent` 未配置；股票列表派生自 5min 表，`list_date / industry` 为空。

**修复方向**  
- 等待 MaxCompute 中财务表、行业表、指数成分股表上线
- `MultiFactorStrategy` 接入真实因子数据

---

*本文档最后更新：2026-05-20*
