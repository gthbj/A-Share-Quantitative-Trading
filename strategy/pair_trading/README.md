# 配对交易策略

同行业统计套利，Kalman Filter 动态对冲，z-score 均值回归。

## 用法

```bash
python run_backtest.py --preset pair_trading
```

## 原理

1. 按板块分组，筛选 Pearson 相关系数 > 0.8 的股票对
2. Engle-Granger 协整检验，ADF p-value < 0.05
3. 估计 OU 过程半衰期，保留 3~20 交易日的对
4. Kalman Filter 动态更新对冲比率
5. |z-score| > 2 开仓，|z-score| < 0.5 平仓

## 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| entry_zscore | 开仓阈值 | 2.0 |
| exit_zscore | 平仓阈值 | 0.5 |
| stop_zscore | 止损阈值 | 3.5 |
| max_holding_days | 最大持仓天数 | 15 |
| use_kalman | 使用 Kalman Filter | true |
| capital_per_pair | 每对资金占比 | 0.1 |

## 依赖

```bash
pip install statsmodels
```
