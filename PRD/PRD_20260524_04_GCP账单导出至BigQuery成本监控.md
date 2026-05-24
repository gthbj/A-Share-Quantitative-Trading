# PRD_20260524_04 GCP 账单导出至 BigQuery 成本监控

## 1. 元信息

| 字段 | 内容 |
|---|---|
| LLM型号 | Claude Opus 4.7 |
| 输出时间戳 | 2026-05-24 08:00:00 |
| 文档编号 | PRD_20260524_04 |
| 关联 Commit | 待提交 |
| 需求优先级 | P2 |

## 2. 背景与动机

随着项目数据基础设施搬到 GCP（PRD_20260522_01 / PRD_20260522_02 / PRD_20260523_07 ~ 12 / PRD_20260524_01 ~ 03 一系列实施），实际产生云成本的组件已不止一个：

- GCS bucket `data-aquarium`：存原始 CSV、`a-share/standardized_parquet` v1 / v2 等多份 Parquet
- BigQuery dataset `ashare`：ODS 外部表 / DWD / DWS / ADS 多层物化结果
- 偶发 GCE VM：PRD_20260523_11 重新生成数据时跑 Parquet build 用过几次
- BigQuery 查询：本地开发与回测会触发 on-demand 扫描

当前对成本只有月底从 GCP Console "Reports" 看到的聚合数字，存在以下问题：

- 无法精确回答"`data-aquarium` bucket 上个月单独花了多少 / 哪一部分（Storage / Class A / Class B / Network）占比多少"
- 无法精确回答"BigQuery 上 `ashare.ods_*` vs `ashare.dwd_*` 各自扫描了多少 TB / 各自存储费多少"
- 当历史回填一次性重跑（如 PRD_11 那次）拉高单日费用时，事后无法用 SQL 复盘
- 没有可被代码或 Notebook 直接查询的成本基线，未来想做"每次回测花多少钱""每个策略实验单价"这类细化分析时无依据

期望行为：开启 Google 官方 **Cloud Billing Export to BigQuery**，把账单明细持续写入 BigQuery，让成本变成可 SQL 查询、可被项目内 Notebook 引用的结构化数据；同时把成本分析查询纳入项目代码库，方便后续复用。

注意本需求**不开发自动告警 / 不构建仪表盘 / 不接入第三方成本管理工具**，第一版只做"接入 + 一组验收 SQL + 文档"。

## 3. 影响模块声明

| 模块 | 是否影响 | 说明 |
|---|---|---|
| `data_layer` | 不影响 | 账单数据与业务数据分 dataset 存放，不进 `BigQueryDataSource` 查询路径 |
| `engine` / `account` / `strategy` / `analytics` | 不影响 | 与交易/回测/策略逻辑完全无关 |
| `config` | 影响 | 新增 `config/gcp_billing.yaml`，记录账单 dataset 名称与几个常用查询参数 |
| `bigquery_pipeline` | 不影响 | 不混入 ODS/DWD/DWS/ADS 数据管道；账单分析查询独立放在 `analytics/cost/` |
| `utils` | 不影响 | |
| `CLI` | 影响（轻）| 新增 `python -m analytics.cost <子命令>` 工具，跑预置 SQL 输出成本报表 |
| `ARCHITECTURE.md` | 影响 | 增加"运维与成本监控"一节，说明账单 dataset 命名 + 与业务 dataset 的隔离原则 |

回测可复现性：本需求不读写业务数据，不可能影响任何已有回测结果。

## 4. 关键文件路径与现有函数签名

本需求**新增文件为主，几乎不改动现有代码**。涉及的少量现有文件：

```
ARCHITECTURE.md                       # 末尾新增章节
PRD/PRD_20260524_04_*.md              # 本 PRD
```

新增：

```
config/gcp_billing.yaml               # 账单 dataset / 表名 / 默认查询区间
analytics/cost/__init__.py
analytics/cost/queries.py             # SQL 模板（按服务/按资源/按日趋势 三组）
analytics/cost/cli.py                 # CLI: monthly-by-service / monthly-by-resource / daily-trend
analytics/cost/README.md              # 使用说明 + Console 配置步骤
```

现有项目里目前没有 `analytics/cost/`，本需求新建。`analytics/` 目录已存在（绩效指标），新加子模块互不干扰。

## 5. 需求详情

### 5.1 功能目标

1. **GCP Console 配置**（人工执行，本 PRD 给步骤即可，不写代码）
   - 在 `data-aquarium` 项目对应的 Billing Account 上开启三类导出：
     - Standard usage cost
     - Detailed usage cost
     - Pricing data
   - 导出目标 dataset：**新建 `gcp_billing`**（与业务 dataset `ashare` 隔离），location 与业务一致：`asia-east2`

2. **配置文件**：`config/gcp_billing.yaml`
   - 记录 dataset 名 / billing account id / 默认查询区间（近 30 天）/ 资源标签命名约定

3. **查询模板**：`analytics/cost/queries.py`
   提供三组参数化 SQL 模板：
   - `monthly_by_service(start_month, end_month)`：按服务汇总月度净成本（cost + credits）
   - `monthly_by_resource(start_month, end_month, service_filter)`：按资源 ID 拆分（依赖 detailed export）
   - `daily_trend(start_date, end_date, service_filter)`：按日成本趋势，用于发现单日异常

4. **CLI 入口**：`python -m analytics.cost <子命令> [参数]`
   - `monthly-by-service --months 3`：打印近 N 个月按服务汇总
   - `monthly-by-resource --month 202605 --service "BigQuery"`：打印某月某服务的资源级明细
   - `daily-trend --days 30`：打印最近 N 天日成本曲线（终端 ASCII 图）

5. **文档**：`analytics/cost/README.md` 描述：
   - Console 开启 Billing Export 的具体步骤（带截图前提的文字步骤）
   - 三种导出表的差异与选用建议
   - 净成本计算公式（必须把 `credits` 数组加进来）
   - 跨区域成本陷阱（dataset location 必须与业务一致）

### 5.2 Console 开启步骤（写进 README）

执行人：项目所有者（需要 Billing Account Administrator 权限），不是 Claude。

1. 打开 GCP Console → 左侧菜单 → **Billing**
2. 选中绑定 `data-aquarium` 项目的 Billing Account
3. 左侧 → **Billing export**
4. 在 "BigQuery export" 卡片，分别为三类导出点击 **Edit settings**：
   - Standard usage cost → 选项目 `data-aquarium`，dataset 选/建 `gcp_billing`，location **`asia-east2`**
   - Detailed usage cost → 同上 dataset
   - Pricing data → 同上 dataset
5. 保存。首批数据约 24 小时内到达；之后每日多次刷新

完成后 dataset 内会自动出现下列表（表名包含 Billing Account ID）：

```
gcp_billing.gcp_billing_export_v1_<BILLING_ACCOUNT_ID>
gcp_billing.gcp_billing_export_resource_v1_<BILLING_ACCOUNT_ID>
gcp_billing.cloud_pricing_export
```

### 5.3 净成本计算约定

所有查询模板必须使用以下公式，避免漏算抵扣：

```sql
SUM(cost) +
SUM(IFNULL((SELECT SUM(amount) FROM UNNEST(credits)), 0))
  AS net_cost
```

并必须包含分区过滤防止全表扫描：

```sql
WHERE DATE(_PARTITIONTIME) >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
```

### 5.4 交互流程

```
[人工 Console 开启]
        ↓ (T+24h)
[BQ 表自动出现数据]
        ↓
python -m analytics.cost monthly-by-service --months 3
        ↓
[终端打印表格：service | net_cost_cny | net_cost_usd]
```

CLI 默认通过 ADC（与 `bigquery_pipeline` 一致的认证路径）连 BigQuery，不重复造认证轮子。

## 6. 配置变更

新增 `config/gcp_billing.yaml`：

```yaml
project_id: "data-aquarium"
billing_dataset: "gcp_billing"
billing_account_id: ""          # 留空，运行时从环境变量或 ADC 项目读取
location: "asia-east2"

tables:
  standard: "gcp_billing_export_v1_${billing_account_id}"
  detailed: "gcp_billing_export_resource_v1_${billing_account_id}"
  pricing: "cloud_pricing_export"

defaults:
  lookback_days: 30
  currency: "USD"               # GCP 账单原币种；如需 CNY 换算在查询层做
```

不修改 `config/backtest.yaml` 或 `bigquery_pipeline/config.yaml`。

## 7. 不可改动的红线区域

- **不修改 `bigquery_pipeline/`、`data_transfer/`、`gcs_to_bigquery/` 任何业务管道代码**
- **不修改 `data_layer/bigquery_source.py`**（账单数据不进回测数据源）
- **不把 `gcp_billing` dataset 与 `ashare` dataset 混用**——业务表与运维表必须分 dataset
- **不在业务表上增加任何 label 写入逻辑**——本 PRD 不动数据写入侧
- **不实现自动告警 / 不接入 Cloud Monitoring 通知**——超出本 PRD 范围
- **不删除任何现有数据或表**

## 8. 修改范围与位置

### 主要新增文件

| 文件 | 内容 |
|---|---|
| `config/gcp_billing.yaml` | 见 §6 |
| `analytics/cost/__init__.py` | 空文件，标记包 |
| `analytics/cost/queries.py` | 三个 SQL 模板函数，返回参数化 SQL 字符串 |
| `analytics/cost/cli.py` | `argparse` CLI，调用 `google.cloud.bigquery` 执行查询并打印 |
| `analytics/cost/README.md` | Console 配置步骤 + 使用示例 + 注意事项 |

### 现有文件最小改动

| 文件 | 改动 |
|---|---|
| `ARCHITECTURE.md` | 末尾追加"运维与成本监控"小节，说明 `gcp_billing` dataset 与 `ashare` 的隔离原则、`analytics/cost/` 的定位 |

### 明确不修改的文件

- `data_layer/**`
- `engine/**`
- `account/**`
- `strategy/**`
- `bigquery_pipeline/**`
- `gcs_to_bigquery/**`
- `data_transfer/**`
- `config/backtest.yaml`

## 9. 验收标准

### 9.1 Console 侧

1. GCP Console → Billing → Billing export 页面，三类 export 状态均为 **Enabled**，目标 dataset 显示 `data-aquarium:gcp_billing`，location `asia-east2`
2. 开启后 48 小时内，`bq ls gcp_billing` 能看到至少 2 张以 `gcp_billing_export_` 开头的表

### 9.2 代码侧

3. `python -m analytics.cost monthly-by-service --months 1` 能成功执行并返回非空结果，至少包含 `BigQuery`、`Cloud Storage` 两行
4. `python -m analytics.cost monthly-by-resource --month <近一月> --service "BigQuery"` 能拆出至少一个 `resource.name`（如某张 BQ 表）
5. `python -m analytics.cost daily-trend --days 7` 能打印 7 行日成本

### 9.3 查询正确性

6. 用例 1：净成本必须扣抵 credits

   ```
   输入：某月 standard export 中 cost=$10.00，credits=[{amount: -$2.00}]
   修改前行为：（没有 CLI）
   修改后预期行为：CLI 输出该月该服务 net_cost = $8.00，而非 $10.00
   ```

7. 用例 2：分区过滤必须存在
   ```
   检查方式：cat analytics/cost/queries.py | grep -E "_PARTITIONTIME|partition"
   预期：所有三个 SQL 模板都包含 _PARTITIONTIME 过滤
   ```

8. 用例 3：跨区域陷阱说明
   ```
   检查方式：阅读 analytics/cost/README.md
   预期：包含"dataset location 必须与业务 dataset 一致，否则查询会触发跨区网络费"这段警示
   ```

### 9.4 不破坏既有功能

9. `pytest tests/` 全量通过，且没有任何新增的失败用例（账单导出对业务零侵入）
10. 现有 `BigQueryDataSource` 默认查询路径完全不变

## 10. 备注

### 10.1 成本估算

按当前项目规模（一个 GCS bucket、一个 BQ dataset、偶发 GCE）：

- Billing export 表自身存储：一年累积约 0.5 GB 量级，存储费 < $0.20/年
- 本 PRD 提供的 SQL 查询：每次扫描 < 100 MB，单次成本 < $0.001
- 都在 BigQuery 每月免费额度（10 GB 存储 + 1 TB 查询）以内

**结论：本需求自身成本接近 $0/年**。

### 10.2 Lookahead Bias 风险

不涉及——账单数据不进回测路径。

### 10.3 历史数据无法回填

开启 Billing Export 之后，**数据从开启时刻起累积**，不会回填历史。如果想分析 2026-05-24 之前的成本，只能用 Console "Reports" 页面手动查看，无法用 SQL。这一点必须在 README 中告知。

### 10.4 后续可扩展但本 PRD 不做

- 把账单数据接入飞书机器人日报 / 周报（独立 PRD）
- 把账单数据与策略实验关联，做"每次回测成本归因"（需先给查询打 label，独立 PRD）
- 跨币种换算到 CNY（独立 PRD，需要决定汇率来源）
- 预算告警 / 异常检测（建议直接用 GCP Budgets 而非自研，独立 PRD）

### 10.5 与其他 PRD 的关系

- 与 PRD_20260523_05 "BigQuery 核心层标准化与成本收尾" 的"成本收尾"是**不同范畴**：PRD_05 关注业务表本身的存储/查询优化（如分区聚簇降低扫描量），本 PRD 关注的是**让所有成本可被观测**。两者互补、不冲突。
- 不依赖 PRD_20260523_07 ~ 12 / PRD_20260524_01 ~ 03 的任何一项，独立可交付。
