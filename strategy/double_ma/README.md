# 双均线策略 (Double MA)

> **类**: `strategy.double_ma.DoubleMAStrategy`
> **包路径**: `strategy/double_ma/`

## 策略原理

经典趋势跟随策略：用短期均线（MA5）和长期均线（MA20）的交叉作为买卖信号。

- **金叉**（买入信号）：MA5 自下而上穿越 MA20，认为短期动能转强 → 满仓买入
- **死叉**（卖出信号）：MA5 自上而下穿越 MA20，认为趋势反转 → 全部清仓
- **止损**：基于持仓加权平均成本价，浮亏超过 5% 触发收盘止损（次日开盘卖出）

## 信号定义

```
设 prev = bar T-1，curr = bar T

金叉触发条件:
    prev.ma_short <= prev.ma_long   AND   curr.ma_short > curr.ma_long
    AND 当前无持仓

死叉触发条件:
    prev.ma_short >= prev.ma_long   AND   curr.ma_short < curr.ma_long
    AND 当前有可卖持仓（T+1 规则下）
```

信号在 bar T 收盘时产生，在 bar T+1 的开盘价撮合成交（`next_open` 模式）。

## 推荐参数

见 `config.yaml`。默认值：

| 参数 | 值 | 说明 |
|---|---|---|
| `short_window` | 5 | 短期均线窗口（15min bar）|
| `long_window` | 20 | 长期均线窗口（15min bar）|
| `universe` | `["510300.SH"]` | 沪深300 ETF |
| `frequency` | `15min` | 15 分钟 K 线 |
| 时间区间 | 2016-01-01 ~ 2025-12-31 | 完整十年 |

## 适用市场

- ✅ **趋势性品种**：宽基指数 ETF（510300/510500/510050）、行业 ETF
- ⚠️ **震荡市场**：会被反复打脸（金叉买入 → 立刻死叉卖出 → 又金叉），频繁交易拉低收益
- ❌ **个股**：双均线对个股噪声敏感，胜率显著低于 ETF

## 已知局限

1. **满仓单标的**：`handle_data` 把全部可用资金压在金叉触发的第一只标的上。多标的回测时先遍历到的会吃光现金，后面的就买不进了。如需多品种实盘，需要重写为等权或风险平价分配。
2. **不区分趋势强弱**：均线交叉本身不带斜率/成交量等强度信号，假突破很多。
3. **参数过拟合风险**：5/20 是经验值，换品种换频率可能需要重新搜参。

## 如何运行

### 一键复现（推荐）
```bash
python run_backtest.py --preset double_ma
```
输出会落到 `strategy/double_ma/runs/{timestamp}/`。

### 覆盖部分参数
```bash
# 改时间窗口
python run_backtest.py --preset double_ma --start 20240101 --end 20240601

# 换标的
python run_backtest.py --preset double_ma --universe 510500.SH

# 多标的（注意上面的"满仓单标的"限制）
python run_backtest.py --preset double_ma --universe "510300.SH,510500.SH"
```

### 完全 ad-hoc（向后兼容旧用法）
```bash
python run_backtest.py --strategy strategy.double_ma.DoubleMAStrategy \
  --start 20240101 --end 20240601 --universe 510300.SH
```

## 历史回测归档

`runs/` 默认 gitignore，但如果想长期保留某次重要的回测产物：

```bash
# 强制把某次 run 加入版本控制
git add -f strategy/double_ma/runs/20260519_192744/summary.md
```

或者直接拷贝关键文件（summary.md / 关键 png）到 `notes/` 子目录手动管理。
