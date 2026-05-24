# ML Stock Picker — 机器学习选股策略

基于 LightGBM / XGBoost 的日线中频选股策略，持仓周期 5~20 天，周频调仓。

## 策略原理

```
特征层：BigQuery DWS 技术特征 + 估值/基本面 + 事件/资金流
    ↓
模型层：预训练 LightGBM 二分类模型（上涨概率）
    ↓
输出层：全市场股票按上涨概率排序 → 取 Top-K 等权持仓
```

### 特征列表

| 类别 | 特征 | 说明 |
|---|---|---|
| 收益率动量 | `return_1d/5d/10d/20d` | 对数收益率 |
| 成交量 | `volume_ma5_ratio` / `volume_ma20_ratio` / `amount_ma5_ratio` | 成交量相对均线倍数 |
| 波动率 | `std_5d` / `std_20d` / `std_ratio` | 标准差及比值 |
| 技术指标 | `rsi_14` | RSI(14) |
| 技术指标 | `macd_diff` / `macd_signal` / `macd_hist` | MACD 三要素 |
| 价格位置 | `close_to_high_20d` | 收盘价在20日高低区间的相对位置 |
| 价格位置 | `close_to_ma5` / `close_to_ma20` | 收盘价偏离均线幅度 |
| 估值/基本面 | `pe_basic` / `pb` / `roe` / `gross_margin` / `net_margin` / `debt_to_assets` | 来自 `dws_equity_fundamental_features` |
| 规模 | `market_cap_log` | 市值对数 |
| 事件/资金流 | `net_inflow_to_amount` / `main_net_inflow_to_amount` / `dragon_tiger_net_to_amount` | 资金流和龙虎榜净额相对成交额 |
| 事件/资金流 | `limit_up_streak` / `is_kpl_event` | 开盘啦榜单事件特征 |

`feature_set=technical` 时保持旧 17 维特征兼容；`feature_set=enhanced` 时使用增强特征。

### 标签构建

- 对每只股票计算未来 `label_horizon`（默认 5）天对数收益。
- 每个交易日截面分位数：top 30% 标记为 1（正样本），bottom 30% 标记为 0（负样本），中间 40% 丢弃。

## 目录结构

```
strategy/ml_stock_picker/
├── strategy.py          # 回测策略（加载模型 → 预测 → 调仓）
├── features.py          # 特征工程（训练与回测共用）
├── train.py             # 模型训练脚本（推荐在 GCP Spot VM 运行）
├── model_storage.py     # 模型本地/GCS 存取
├── config.yaml          # 回测 preset 配置
├── train_config.yaml    # 训练配置
└── README.md            # 本文件
```

## 使用流程

### 1. 训练模型

```bash
# 本地训练（数据量小时）
python strategy/ml_stock_picker/train.py \
  --config strategy/ml_stock_picker/train_config.yaml

# 输出
# strategy/ml_stock_picker/models/lgbm_model.pkl
# strategy/ml_stock_picker/models/lgbm_model.metrics.json
```

训练配置要点：
- `data_source`: `bigquery_dws`（推荐）、`bigquery` 或 `local`
- `train.feature_set`: `enhanced`（推荐）或 `technical`
- `train.validation_ratio`: 按时间尾部切分验证集，避免随机打散导致未来泄漏
- `train.start_date` / `end_date`: 训练区间，建议至少 2 年
- `train.model_type`: `lightgbm`（推荐）或 `xgboost`
- `train.model_output_path`: 模型保存路径，支持 `gs://bucket/models/xxx.pkl`

### 2. 回测

```bash
# 使用 preset 一键回测
python run_backtest.py --preset ml_stock_picker

# 或覆盖参数
python run_backtest.py \
  --preset ml_stock_picker \
  --start 20220101 --end 20231231 \
  --universe "000001.SZ,600519.SH"
```

回测参数（`config.yaml` 中 `params`）：
- `model_path`: 预训练模型路径
- `model_type`: 与训练时一致
- `feature_source`: `auto` / `dws` / `local`，默认优先 DWS，失败后回退本地技术特征
- `feature_set`: `enhanced` / `technical`
- `use_deterministic_fallback`: 模型缺失或预测失败时使用确定性增强打分
- `top_k`: 持仓数量（默认 8）
- `rebalance_freq`: 调仓频率，交易日（默认 5，周频）
- `position_pct`: 资金使用比例（默认 95%）

### 3. 模型部署到 GCS（可选）

```bash
# 上传模型到 Cloud Storage
gsutil cp strategy/ml_stock_picker/models/lgbm_model.pkl \
  gs://data-aquarium/models/lgbm_model.pkl

# 修改 config.yaml / train_config.yaml 中的 model_path
gs://data-aquarium/models/lgbm_model.pkl
```

## 关键设计决策

| 决策 | 说明 |
|---|---|
| 模型训练与回测分离 | 训练在 GCP Spot VM 完成，回测只加载 `.pkl` 模型，避免每次回测重复训练 |
| T+1 开盘成交 | 回测默认 `next_open`，信号基于收盘后特征，次日开盘执行，避免未来函数 |
| 周频调仓 | `rebalance_freq=5`，控制换手率，降低佣金侵蚀 |
| 等权持仓 | Top-K 股票各买 1/K 仓位，简化执行，避免单只过度集中 |
| 截面标签 | 每天独立排序生成正负样本，适应市场 beta 变化 |
| DWS 优先 | 回测调仓日优先读取 `ashare.dws_*` 快照，训练与回测使用同一套字段口径 |
| 确定性 fallback | 模型不存在时使用可解释 score，不再随机选股，保证回测可复现 |

## 性能参考

| 指标 | 参考值 |
|---|---|
| 训练数据 | 40 只 × 3 年 ≈ 30,000 条日线；全市场训练可直接读取 DWS |
| 训练时间 | LightGBM CPU 小样本约 5-15 秒，全市场取决于查询规模 |
| AUC | 0.55 ~ 0.65（中频策略常见水平） |
| RankIC | 0.03 ~ 0.08 |

## 待优化

- [ ] 加入行业中性化约束（等权行业内选股）
- [ ] 滚动训练（walk-forward）：每月用最新数据重新训练
- [ ] 模型 ensemble：5-10 个 LightGBM 模型平均
- [ ] 超参搜索：使用 `optuna` 自动寻优
- [ ] SHAP 特征重要性分析
