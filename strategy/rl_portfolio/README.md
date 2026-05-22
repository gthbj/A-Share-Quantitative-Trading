# RL 组合权重管理策略

PPO 强化学习做仓位管理。

## 用法

```bash
# 训练模型
python strategy/rl_portfolio/train.py \
  --returns-csv data/market_returns.csv \
  --timesteps 10000 \
  --save-path strategy/rl_portfolio/models/ppo_v1.zip

# 回测
python run_backtest.py --preset rl_portfolio
```

## 参数

| 参数 | 说明 | 默认值 |
|------|------|--------|
| model_path | 模型路径 | strategy/rl_portfolio/models/ppo_v1.zip |
| observation_window | 观测窗口 | 5 |
| rebalance_freq | 调仓频率 | 1 |

## 依赖

```bash
pip install stable-baselines3
```
