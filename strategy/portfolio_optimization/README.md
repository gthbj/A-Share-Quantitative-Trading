# 组合优化策略

基于 CVXPY + NumPy 的组合权重优化，支持风险平价、最小方差、最大分散化三种方法。

## 用法

```bash
python run_backtest.py --preset portfolio_optimization
```

## 原理

- **最小方差**：最小化组合方差，对预期收益估计误差不敏感
- **风险平价**：让每个资产对组合风险的贡献相等
- **最大分散化**：最大化分散化比率

## 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| method | 优化方法 | risk_parity |
| lookback | 协方差估计窗口 | 60 |
| rebalance_freq | 调仓频率（交易日） | 5 |
| max_weight | 单只权重上限 | 0.20 |
| min_weight | 单只权重下限 | 0.0 |

## 依赖

```bash
pip install cvxpy
```
