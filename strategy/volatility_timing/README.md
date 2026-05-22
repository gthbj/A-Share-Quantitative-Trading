# GARCH 波动率择时策略

GARCH(1,1) 预测基准指数波动率，在预测波动率高时降低仓位。

## 用法

```bash
python run_backtest.py --preset volatility_timing
```

## 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| high_volile | 高波动分位数 | 0.80 |
| low_volile | 低波动分位数 | 0.20 |
| high_vol_position | 高波动时仓位 | 0.5 |
| low_vol_position | 低波动时仓位 | 1.0 |

## 依赖

```bash
pip install arch
```
