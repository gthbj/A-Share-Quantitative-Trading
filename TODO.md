# A-Share Quantitative Trading — 已知问题与待优化清单

> **维护原则**：此文档记录已发现、但尚未修复的问题。每项需包含：现象描述、根因分析、建议修复方向、优先级。

---

## 🔴 P0 — 高优先级（影响核心功能正确性）

### P0-1: 收益曲线/回撤图出现大面积水平线段（纯平）

**现象**  
HTML 报告中的累计收益折线图与回撤曲线图出现明显的大段水平直线。例如 `cum_returns.png` 中 2023-01 之后近 10 个月净值完全不变，2022 年中也有多段长达数周的水平线。

**根因分析**  
`_build_result_df()` 中日期解析逻辑存在缺陷：

```python
raw_dates = df["date"].astype(str)
df["date"] = pd.to_datetime(raw_dates, format="%Y%m%d%H%M", errors="coerce")
mask = df["date"].isna()
if mask.any():
    df.loc[mask, "date"] = pd.to_datetime(
        raw_dates[mask].str.slice(0, 8), format="%Y%m%d", errors="coerce"
    )
```

日线回测的日期格式为 `YYYYMMDD`（8 位），但代码优先尝试用 `%Y%m%d%H%M`（12 位）解析。在 Python 3.9 + pandas 2.3.3 环境下，`strptime('20231218', '%Y%m%d%H%M')` 不会抛出 `ValueError`，而是被错误解析为 `2023-01-02 01:08:00`。这导致：
1. 大量不同交易日的记录被映射到同一个错误时间戳（如 `2023-01-02` 的多个分钟）；
2. 同一日的多条记录 nav 值相同，matplotlib 连线后表现为水平线段；
3. 真实交易日期间的记录缺失，导致曲线中间出现断裂或长平线。

**验证**  
```python
>>> pd.to_datetime('20231218', format='%Y%m%d%H%M', errors='coerce')
2023-01-02 01:08:00   # 应为 NaT
```

**修复方向**  
- 根据字符串长度显式路由：长度 8 → `%Y%m%d`；长度 12 → `%Y%m%d%H%M`；其他长度 → 报错或 `NaT`。
- 或者不再用 `format` + `errors='coerce'` 的 fallback 模式，直接按长度选择解析器。

**影响范围**  
所有日报/周报的可视化输出，导致用户无法正确判断策略收益走势与回撤节奏。

---

### P0-2: 止损单执行时机存在未来函数（Lookahead Bias）

**现象**  
PRD_20260510_02 要求"次日开盘价执行"，但当前实现中止损检查发生在当日收盘后/当日最后一个 Bar 后，生成的 MARKET 卖出单在同一日的 `TradeEngine.execute_orders()` 中被撮合。这意味着止损触发时已经知道了当日收盘价，却以当日价格成交。

**根因分析**  
`_check_stop_loss()` 在 `_run_daily` 的当日循环末尾调用，止损单被直接追加到当日订单列表中执行，没有延迟到下一交易日。

**修复方向**  
- 将止损检查生成的订单存入 `_stop_loss_pending` 队列；
- 下一交易日开盘前（`before_trading_start` 之后，第一个 Bar 之前）将 pending 订单加入 context；
- 分钟级回测同理：在下一交易日第一个分钟 Bar 时执行前一日收盘触发的止损单。

**影响范围**  
止损开启的回测结果可能过于乐观，低估实际滑点与次日跳空风险。

---

## 🟡 P1 — 中优先级（影响体验或特定场景）

### P1-1: AKShare 分钟级数据网络不可达

**现象**  
当前运行环境无法访问 AKShare 的分钟级行情接口（EastMoney API 被屏蔽），回测被迫使用 `_simulate_minute_bars_from_daily()` 生成的模拟分钟数据（seed=42）。

**根因分析**  
网络环境限制，非代码问题。

**修复方向**  
- 短期：增加更多 fallback 数据源（如 Tushare Pro、本地 CSV 导入）；
- 长期：接入付费数据商或自建数据中心。

**影响范围**  
仅影响分钟级回测的真实性；日线回测使用 AKShare 日 K 接口，不受此影响。

---

### P1-2: Matplotlib 中文标签渲染为警告/方框

**现象**  
图表标题、轴标签中的中文字符（如"累计收益率"）因系统缺少 CJK 字体，渲染为方框或触发 `UserWarning: Glyph ... missing from font(s) DejaVu Sans.`

**根因分析**  
macOS 默认未安装支持中文的 Matplotlib 字体。

**修复方向**  
- 在 `Plotter` 初始化时自动检测并设置系统可用的中文字体（如 `Arial Unicode MS`、`PingFang SC`、`SimHei` 等）；
- 或打包一个开源中文字体到项目中，并在运行时动态加载。

**影响范围**  
纯视觉问题，不影响数据计算，但降低报告可读性。

---

## 🟢 P2 — 低优先级（建议优化）

### P2-1: 交易日历为硬编码假期

**现象**  
`utils/calendar.py` 中 2020-2024 年的长假为硬编码列表，2025 年及以后的数据需要手动维护。

**修复方向**  
接入 `exchange_calendars` 库或 AKShare 的交易日历接口，自动获取全量历史与未来交易日历。

---

### P2-2: 缺少单元测试覆盖

**现象**  
项目目前无自动化测试，回归验证依赖手工运行全量回测。

**修复方向**  
- 为 `TradeEngine`、`Portfolio`、`Position` 等核心类编写 pytest 单元测试；
- 为 `_build_result_df`、止损逻辑等易出 bug 的边界场景增加专项测试。

---

*本文档最后更新：2026-05-10*
