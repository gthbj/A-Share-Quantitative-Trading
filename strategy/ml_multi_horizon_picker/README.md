# ML Multi-Horizon Picker — 多 Horizon 择时择股策略

基于 4 个 LightGBM 买入模型（1/5/10/20 天 horizon）+ 1 个独立卖出风险模型 + regime 调制的日频策略。配套 PRD：`PRD/PRD_20260524_05_*`。

与 `ml_stock_picker/` 的关键区别：

| 维度 | ml_stock_picker | **ml_multi_horizon_picker**（本策略）|
|---|---|---|
| 模型数 | 1（固定 horizon=5）| **5**（4 buy × horizon + 1 sell）|
| 调仓频率 | 5 日 | **每日重新评分** |
| 持有期 | 5 日固定 | **动态**：argmax_h h × prob_up_h |
| 卖出触发 | 跌出 Top-K | **6 个独立触发**（见下）|
| 市场状态 | 无 | **regime 三态**（调制持仓数与止损宽度）|
| 风险控制 | 无 | 硬止损 + 追踪止盈 + sell 模型 |

## 卖出触发（任一命中即卖）

| 触发 | 规则 | 默认参数 |
|---|---|---|
| **a. 硬止损** | 现价 < cost × (1 − stop_loss_pct) | bull/neutral: -5%, bear: -3% |
| **b. 追踪止盈** | 现价 < 持仓期高点 × (1 − trailing_stop_pct) | -3% from peak |
| **c. Horizon 到期** | 持有天数 ≥ 建仓时锁定的 expected_horizon | argmax_h h × prob_up_h |
| **d. 排名迟滞** | 持有 ≥ min_hold_days 且连续 ≥ dropout_persistence_days 不在 Top-2K | 3 天 / 2 天 |
| **e. prob_up 兜底** | prob_up_h5 < min_prob_floor 且持有 ≥ min_hold_days | 0.30 |
| **f. 卖出模型** | prob_sell > sell_threshold | 0.70 |

## Regime 调制

基于基准 `000300.SH`：

| Regime | 判定 | 持仓数 | 止损 |
|---|---|---|---|
| **Bull** | close > MA200 且 60d vol < 80% 分位 | 100% × N | 5% |
| **Neutral** | close > MA200 且 60d vol ≥ 80% 分位 | 70% × N | 5% |
| **Bear** | close ≤ MA200 | **30% × N + 不开新仓** | 3% |

## 使用流程

### 1. 训练模型（可选——未训练时走 deterministic fallback）

```bash
export ASHARE_USE_GCLOUD_ACCESS_TOKEN=1   # 复用 gcloud token 认证
python -m strategy.ml_multi_horizon_picker.train \
  --config strategy/ml_multi_horizon_picker/train_config.yaml
```

输出：`models/ml_multi_horizon/{buy_h1,buy_h5,buy_h10,buy_h20,sell_v1}.pkl` + `metadata.json`

### 2. 回测

```bash
python run_backtest.py --preset ml_multi_horizon_picker \
  --start 20240301 --end 20240930 \
  --capital 1000000
```

注意：`feature_window=20`，回测期前 ~20 个交易日因数据不足会自动跳过（normal warmup）。

### 3. 只训卖出模型 / 只训买入模型

```bash
python -m strategy.ml_multi_horizon_picker.train --config ... --skip-buy
python -m strategy.ml_multi_horizon_picker.train --config ... --skip-sell
```

## 设计权衡

### 为什么 4 个独立 buy 模型而不是一个多任务模型？

- 独立模型可独立 early-stopping / 调参，避免长 horizon 过拟合短 horizon
- 易于 ablation（如只用 h10 + h20 时）
- 训练 + 推理总计 < 5 分钟，计算成本可忽略

### 为什么 score = max(h5, h10, h20)，跳过 h1？

- h1 太噪：日内 momentum 难超越随机
- 跨 horizon max 实现"对短/中/长期任一都看好"的或语义
- 动态 holding = argmax_h h × prob_up_h，让长 horizon 高确信度的持有更久

### 为什么追踪止盈而非固定止盈？

- 固定止盈（如 +10% 卖）会过早砍掉牛股
- 追踪止盈从持仓期高点回撤 3% 才卖，让趋势走完

### 为什么 sell 模型 label 用最大回撤而非"未来 5 日收益 < 0"？

- 我们关心的是**下行风险**而非整体收益方向，回撤标签更直接
- 与买入模型互补：买入模型预测"会涨"，sell 模型预测"会大跌"，两者非冗余

### 为什么 bear regime 不直接全部清仓？

- 仍保留 30% 仓位（仅最强信号），避免错过反弹
- 已持有的不强制卖；新开仓暂停。让自然的卖出触发收尾

## 已知限制

- **CSI300 作为唯一 regime 指标**：可能滞后于行业轮动
- **特征只用技术面**（17 维 + 5 维风险）：尚未接入估值/基本面（未来可加 `dws_equity_fundamental_features`）
- **训练区间样本不平衡**：bear 标签可能稀疏，需考察类别权重
- **回测前 20 天空跑**：feature_window 限制；目前没有改造 engine 做 warmup 预加载

## 后续改造方向

详见 PRD §10.3。一句话：v1 求跑通，v2 才求性能。

## 文件清单

```
strategy/ml_multi_horizon_picker/
├── __init__.py           — 模块导出
├── README.md             — 本文件
├── features.py           — 17 + 5 维特征工程（17 维复用 ml_stock_picker）
├── labels.py             — 多 horizon + 卖出标签生成
├── regime.py             — bull / neutral / bear 检测
├── model_storage.py      — 5 模型分文件存/取
├── train.py              — 一键训练入口
├── strategy.py           — 主回测策略（6 sell trigger + regime 调制）
├── config.yaml           — 回测 preset
└── train_config.yaml     — 训练配置
```

测试：`tests/test_strategy_ml_multi_horizon.py`（19 用例，覆盖 labels / features / regime / 6 个 sell trigger）。
