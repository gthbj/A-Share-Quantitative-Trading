# ML Rich Picker — 富特征 ML 择时择股策略

在 [`ml_multi_horizon_picker`](../ml_multi_horizon_picker/) (v1) 基础上**扩展特征维度**：

| 维度 | v1 (ml_multi_horizon) | **v2 (ml_rich_picker)** |
|---|---|---|
| Buy 特征 | 17（日线技术）| **30**（17 daily + 8 fundamental + 5 event）|
| Sell 特征 | 22（17 + 5 risk）| **39**（30 + 5 risk + 4 position state）|
| 数据源表 | 1（`dws_equity_daily_features`）| **3**（+ `dws_equity_fundamental_features`, `dws_equity_event_money_flow_features_1d`）|
| 模型架构 | 4 horizon buy + 1 sell | **同**（完全继承）|
| Sell 触发 | 止损/追踪止盈/排名/prob 兜底/sell 模型/max_hold 兜底 | **继承新持仓决策口径：无 h5 到期硬卖** |
| Regime 调制 | bull/neutral/bear | **同**（完全继承）|

配套 PRD：[`PRD/PRD_20260525_03_*`](../../PRD/PRD_20260525_03_富特征ML策略.md)。

---

## 新增特征列表

### 基本面 8 维（`dws_equity_fundamental_features`）

| 列 | 含义 |
|---|---|
| `pe_basic` | 基本市盈率（负值表亏损）|
| `pb` | 市净率 |
| `roe` | 净资产收益率 |
| `gross_margin` | 毛利率 |
| `net_margin` | 净利率 |
| `debt_to_assets` | 资产负债率 |
| `log_market_cap` | `LN(market_cap+1)`（小盘溢价信号）|
| `asset_turnover` | 资产周转率 |

### 资金流/事件 5 维（`dws_equity_event_money_flow_features_1d`）

| 列 | 计算 | 含义 |
|---|---|---|
| `net_inflow_pct` | `net_inflow_amount / amount` | 净流入占成交额 |
| `main_net_inflow_pct` | `main_net_inflow_amount / amount` | 主力净流入占比 |
| `dragon_tiger_net_pct` | `dragon_tiger_net_amount / amount` | 龙虎榜净买入占比 |
| `limit_up_streak` | 直接 | 连续涨停天数 |
| `is_kpl_event_int` | `CAST(is_kpl_event AS INT64)` | 开盘啦事件 0/1 |

---

## 使用流程

### 1. 训练模型（走步重训）

```bash
export ASHARE_USE_GCLOUD_ACCESS_TOKEN=1

# 本地跑（~3-5 小时，500 股 × 64 时点）
python -m strategy.ml_rich_picker.walk_forward \
    --config strategy/ml_rich_picker/walk_forward_config.yaml

# 或 Cloud Run 并行（~30 分钟，~HK$3）
./deploy/cloud_run_walk_forward_rich/run.sh
```

输出：`models/walk_forward_rich/{YYYYMMDD}/{buy_h1,buy_h5,buy_h10,buy_h20,sell_v1}.pkl`

### 2. 回测

```bash
python run_backtest.py --preset ml_rich_picker \
    --start 20200102 --end 20250430 --capital 1000000
```

策略 `initialize` 会一次性预拉取整个回测期 + 60 天 warmup 的富特征宽表
（3 表 JOIN），handle_data 时 O(1) 查表打分。**比 v1 推理快 10-20 倍**
（v1 每天 500 股 × context.get_price 调用；v2 一次 BQ 拉完）。

### 3. 与 v1 A/B 对比

```bash
# v1 跑（已有模型）
python run_backtest.py --preset ml_multi_horizon_picker --start ... --end ...

# v2 跑
python run_backtest.py --preset ml_rich_picker --start ... --end ...

# 对比 strategy/ml_*/runs/*/summary.md 两份报告
```

---

## 设计要点

### 为什么基本面 + 资金流单独 JOIN，不放进 daily features 表？

- `dws_equity_daily_features` 是技术面专用，schema 稳定
- 基本面更新频率低（季报），有自己的 partition 逻辑（announcement_date）
- 资金流是事件型数据，部分日期有部分日期没有
- JOIN 在 SQL 层做（不在 Python 层），效率高、内存友好

### LEFT JOIN 缺失值不填充

LightGBM 原生支持 NaN，不做均值/前向填充：

- 避免引入 lookahead bias（用未来均值填过去）
- 缺失本身往往是信号（新股 / 龙虎榜未上榜 / 季报未出）
- 模型可学到"缺失 = 信号"的语义

### 推理时预加载所有特征

v1 在 handle_data 里 for-loop 调用 `context.get_price(code)` × 500 股，
每天约 1-2 秒。5 年回测 1300 天 × 2 秒 ≈ 40-60 分钟。

v2 在 initialize 里一次拉 5 年 × 500 股的全部特征（约 30 MB），
然后查表 O(1)。5 年回测 < 5 分钟。

### 不重新训练 sell 模型？

sell 模型使用 rich sell 特征（39 维输入，含持仓状态特征）重新训练。
所以 sell 模型有自己的 .pkl，与 v1 模型不冲突。

---

## 文件清单

```
strategy/ml_rich_picker/
├── __init__.py
├── README.md             — 本文档
├── features.py           — 富特征列定义 + deterministic fallback
├── walk_forward.py       — 3 表 JOIN + 走步训练（复用 v1 框架）
├── strategy.py           — 主回测策略（继承 MLMultiHorizonStrategy）
├── config.yaml           — 回测 preset
└── walk_forward_config.yaml  — 训练配置

deploy/cloud_run_walk_forward_rich/
├── Dockerfile
├── cloudbuild.yaml
├── walk_forward_rich_cloud_config.yaml
└── run.sh                — 一键 Cloud Run 部署

tests/test_strategy_ml_rich_picker.py
```

## 与 v1 的关系

- **不修改 v1 任何文件**（PRD §7 红线）
- 大量 import 复用 v1 的 labels / regime / model_storage / model_registry / tradable
  / walk_forward 辅助函数
- 模型存储路径完全独立：`models/walk_forward_rich/` vs v1 `models/walk_forward/`
- 两套策略可以并行训练 / 并行回测，做 A/B 对比

## 待完成（v2 不做）

- 行业内 cross-sectional 归一化（PE 相对行业的水位）
- 财报 release 日特殊处理
- ESG / 分析师预期 等外部数据
- Multi-task learning（同模型同时输出 buy/sell）

详见 PRD §10.4。
