# HMM 市场状态切换策略

用 GaussianHMM 识别市场隐藏状态（Bull/Bear/Sideways），根据状态调整目标仓位。

## 用法

```bash
python run_backtest.py --preset regime_switching
```

## 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| n_states | 隐状态数 | 3 |
| lookback | 训练窗口 | 252 |
| observation_window | 观测窗口 | 20 |
| position_pcts | 各状态仓位 | [1.0, 0.5, 0.0] |

## 依赖

```bash
pip install hmmlearn
```
