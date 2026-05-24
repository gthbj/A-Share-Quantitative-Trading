# analytics/cost — GCP 账单成本分析

把 GCP 的费用明细导出到 BigQuery，并提供 CLI / SQL 模板做成本拆分。

配套 PRD：[`PRD/PRD_20260524_04_GCP账单导出至BigQuery成本监控.md`](../../PRD/PRD_20260524_04_GCP账单导出至BigQuery成本监控.md)。

---

## 一、Console 配置（一次性，5 分钟）

### 1. 拿到 Billing Account ID

打开 [Billing Projects 页面](https://console.cloud.google.com/billing/projects?project=data-aquarium)，
找到绑定 `data-aquarium` 的 Billing Account，复制其 ID（形如 `01ABCD-23EFGH-45IJKL`）。

### 2. 开启三类导出

进入 [Billing Export 页面](https://console.cloud.google.com/billing/export)，
顶部选中刚才的 Billing Account，在 "BigQuery export" 区块对三类导出分别点 "Edit settings"：

| 导出类型 | 用途 |
|---|---|
| **Standard usage cost** | 每条 SKU 级费用，按服务/项目/标签/区域分摊 |
| **Detailed usage cost** | 在 Standard 基础上**精确到资源 ID**（具体 VM / bucket / BQ 表） |
| **Pricing data** | GCP 全量 SKU 定价表（每日刷新） |

每一类都填：

- **Project**：`data-aquarium`
- **Dataset**：第一次需点 "CREATE NEW DATASET"
  - Dataset ID：`gcp_billing`
  - **Data location：`asia-east2`** ⚠️ 必须，与业务 dataset `ashare` 同区域
  - Default table expiration：留空

三类保存后，状态都应显示 "Enabled"。

### 3. 等 24 小时

首批数据约 24 小时内到达，之后每日多次刷新。可以用
[BigQuery Console](https://console.cloud.google.com/bigquery?project=data-aquarium)
进入 `gcp_billing` dataset 确认这三张表已自动创建：

```
gcp_billing_export_v1_<BILLING_ACCOUNT_ID>           # standard
gcp_billing_export_resource_v1_<BILLING_ACCOUNT_ID>  # detailed
cloud_pricing_export                                  # pricing
```

⚠️ 注意 Billing Account ID 中的 `-` 在表名里会变成 `_`。

---

## 二、本地配置

把 `config/gcp_billing.yaml` 中的 `billing_account_id` 填好：

```yaml
billing_account_id: "01ABCD-23EFGH-45IJKL"   # 你的真实 ID
```

或通过环境变量供给（优先级高于 config 文件）：

```bash
export GCP_BILLING_ACCOUNT_ID="01ABCD-23EFGH-45IJKL"
```

认证走 ADC（Application Default Credentials），与 `bigquery_pipeline` 一致：

```bash
gcloud auth application-default login
```

---

## 三、CLI 使用

### 近 3 个月各服务花费

```bash
python -m analytics.cost monthly-by-service --months 3
```

示例输出：

```
invoice_month | service        | net_cost | currency
--------------+----------------+----------+---------
2026-05       | BigQuery       | 1.2345   | HKD
2026-05       | Cloud Storage  | 0.5678   | HKD
2026-04       | BigQuery       | 0.9876   | HKD
...
```

### 某月某服务的资源级明细

```bash
python -m analytics.cost monthly-by-resource --month 202605 --service "BigQuery"
```

示例输出：

```
service  | sku                | resource_name        | resource_global_name | net_cost | currency
---------+--------------------+----------------------+----------------------+----------+---------
BigQuery | Active Storage     | ashare.dwd_...       | ...                  | 0.5      | HKD
BigQuery | Analysis           | (none)               |                      | 0.7      | HKD
...
```

### 日成本趋势（最近 7 天）

```bash
python -m analytics.cost daily-trend --days 7
```

示例输出（ASCII 柱状图）：

```
usage_date  |   net_cost | trend
2026-05-18  |     0.0231 | ████████··············
2026-05-19  |     0.0312 | ███████████···········
2026-05-20  |     0.0589 | █████████████████████
2026-05-21  |     0.0102 | ████··················
...
```

### dry-run 只看 SQL 不执行

```bash
python -m analytics.cost monthly-by-service --months 1 --dry-run
```

---

## 四、注意事项

### 净成本必须含 credits

GCP 账单的 `cost` 是毛费用，`credits` 是抵扣（如 free tier、promo）。**净成本 = cost + sum(credits.amount)**（credits.amount 本身为负数）。本模块所有 SQL 都已包含这一计算。

### 必须带 `_PARTITIONTIME` 过滤

账单表按日分区。**所有查询必须包含 `WHERE DATE(_PARTITIONTIME) >= ...`** 防止全表扫描。本模块所有 SQL 模板都已包含。

### 跨区域陷阱

`gcp_billing` dataset **必须**与业务 dataset `ashare` 在同一区域（`asia-east2`）。否则跨区查询会触发出口网络费，且 BigQuery 不允许同一 query 跨区 JOIN。

### 历史数据无法回填

Billing Export 开启后**从启用时刻起累积**，不会回填历史。如需分析启用前的成本，只能用
[Console Reports 页面](https://console.cloud.google.com/billing/reports?project=data-aquarium)
手动看。

### 与业务 dataset 严格隔离

`gcp_billing` dataset **不要**和 `ashare` 混用，也**不要**让 `BigQueryDataSource` 读它——账单是运维数据，不进回测路径。

---

## 五、成本

按当前项目规模：

- 账单表自身存储：< 0.5 GB/年 → < $0.20/年
- 本模块 SQL 查询：单次扫描 < 100 MB → 单次 < $0.001
- 在 BigQuery 每月免费额度（10 GB 存储 + 1 TB 查询）内

**结论：本模块自身成本接近 $0/年。**
