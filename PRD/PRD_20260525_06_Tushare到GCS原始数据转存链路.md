# PRD_20260525_06 Tushare 到 GCS 原始数据转存链路

## 1. 元信息

| 字段 | 内容 |
|---|---|
| LLM型号 | GPT-5 Codex |
| 输出时间戳 | 2026-05-25 CST |
| 文档编号 | PRD_20260525_06 |
| 关联 Commit | 待提交 |
| 需求优先级 | P0 |
| 当前分支 | `codex/tushare-data-source-prd` |
| 当前 worktree | `.claude/worktrees/tushare-data-source-prd` |

## 2. 目标

建立一条稳定、可断点续传、可审计的 Tushare Pro 到 GCS 的数据转存链路。

第一版只解决：

1. 从 Tushare API 按配置拉取指定数据。
2. 将接口返回数据以 append-only Parquet 形式写入 GCS。
3. 保存 manifest、checkpoint、ingestion metadata，支持失败后续跑。
4. 按 p0、p1、p2 优先级从 20190101 拉到当前日期。
5. 为后续 BigQuery、特征工程、模型训练保留足够的原始数据和审计信息。

本 PRD 不讨论 BigQuery 建表、DWD/DWS/ADS、训练集生成、模型重训、回测、实盘信号和旧模型替换。这些是后续 PRD 的范围。

## 3. 背景结论

Tushare 不适合作为训练、回测或生产策略的在线直接数据源，原因是：

- API 有积分、频次、单次行数和独立权限限制。
- 上游历史数据可能修正，直接在线读取会破坏可复现性。
- 大规模训练和回测需要稳定快照，不能依赖实时 API 成功率。
- 前复权价格可能随未来分红送转被回写，必须保存可审计的原始数据和拉取时间。

因此当前阶段只把 Tushare 定位为上游采集源，GCS raw landing 是项目内部后续处理的正式入口。

## 4. 范围

### 4.1 本阶段包含

- Tushare token 读取和安全使用。
- Endpoint 配置化。
- p0/p1/p2 优先级调度。
- 20190101 至当前日期的历史回补。
- API 限频，当前按用户账号上限 `120 次/分钟` 执行。
- 失败重试。
- 单次行数限制检测。
- 支持 `limit` / `offset` 形式的分页。
- GCS raw Parquet 写入。
- GCS manifest 写入。
- GCS checkpoint 写入，用于断点续传。
- 可选 standardized Parquet 写入开关，默认关闭。
- Cloud Run Job 部署脚本。
- 本地 dry-run / plan / smoke test 能力。

### 4.2 本阶段不包含

- 不创建新的 BigQuery dataset 或 table。
- 不做 GCS 到 BigQuery 加载。
- 不定义 DWD/DWS/ADS 表结构。
- 不生成训练特征。
- 不训练模型。
- 不修改策略、回测、撮合、账户逻辑。
- 不接阿里云 MaxCompute。
- 不全量接分钟线、Tick、公告全文、研报全文。
- 不让任何训练或回测流程直接调用 Tushare API。

## 5. GCS 目标路径

根路径：

```text
gs://data-aquarium/a-share/tushare/
```

第一版实际目录：

```text
gs://data-aquarium/a-share/tushare/
  raw/
    api=<api_name>/
      endpoint=<endpoint_key>/
        partition_date=<YYYYMMDD>/
          run_id=<YYYYMMDDTHHMMSSZ>/
            <endpoint_key>_<logical_date>_<hash>.parquet

  _manifests/
    run_id=<run_id>/
      records/
        000001_<endpoint_key>_<logical_date>.json
      summary.json

  _checkpoints/
    endpoint=<endpoint_key>/
      logical_date=<YYYYMMDD>.json

  standardized_parquet/       # 默认关闭，后续需要时再开启
    <target_table>/
      partition_month=<YYYYMM|all>/
        run_id=<run_id>/
          <endpoint_key>_<logical_date>_<hash>.parquet
```

说明：

- `raw` 是必须保留的原始接口快照。
- `_manifests` 是每次运行的任务账本，用于审计和排错。
- `_checkpoints` 是断点续传状态，用于跨 run 跳过已完成任务。
- `standardized_parquet` 第一版默认不物理双写，避免重复存储；后续可从 raw 回放生成。

## 6. Raw 数据字段要求

写入 raw Parquet 时保留 Tushare 原始返回字段，并追加以下元数据字段：

| 字段 | 说明 |
|---|---|
| `_source` | 固定为 `tushare`。 |
| `_tushare_api` | 实际调用的 Tushare API，例如 `daily`、`income_vip`。 |
| `_endpoint_key` | 当前配置中的 endpoint，例如 `stock_basic_listed`。 |
| `_target_table` | 后续标准层目标表名，仅作元数据。 |
| `_run_id` | 本次运行批次。 |
| `_ingested_at` | 数据写入时间，UTC ISO 格式。 |
| `_logical_date` | 本任务的逻辑日期。 |
| `_request_params_json` | 本次请求参数 JSON，不包含 token。 |

这些字段用于：

- 证明数据何时被拉取。
- 回放任意 endpoint。
- 排查参数和数据差异。
- 为后续 PIT 审计保留证据。

## 7. Endpoint 定义

`endpoint` 是一个具体的数据拉取任务配置，不等同于 Tushare 的 `api_name`。

示例：

```yaml
stock_basic_listed:
  api_name: "stock_basic"
  target_table: "dim_security"
  mode: "snapshot"
  params:
    exchange: ""
    list_status: "L"
```

含义：

| 字段 | 说明 |
|---|---|
| `endpoint_key` | 本地任务名，例如 `stock_basic_listed`。 |
| `api_name` | 实际调用的 Tushare API，例如 `stock_basic`。 |
| `params` | 调用 API 时传入的固定参数。 |
| `mode` | 任务拆分方式，例如按交易日、自然日、季度、快照。 |
| `date_param` | 传给 Tushare 的日期参数名。 |
| `target_table` | 后续标准化或 BigQuery 的目标表名，仅作为元数据。 |
| `priority` | 当前调度优先级，例如 `p0`、`p1`、`p2`。 |

同一个 `api_name` 可以有多个 endpoint。例如 `stock_basic` 被拆成上市、退市、待上市三个 endpoint，避免不同参数结果混在一起。

## 8. 任务拆分模式和分区规则

raw 路径里的 `partition_date` 来自 `logical_date`。

| mode | 任务拆分方式 | `logical_date` / `partition_date` |
|---|---|---|
| `snapshot` | 每个 endpoint 只拉一次当前快照 | 本次 `run_id` 对应日期；同一 run 内冻结不变。 |
| `date_range` | 一个请求覆盖 `start_date` 到 `end_date` | 请求区间的 `end_date`。 |
| `by_year_range` | 按自然年切分 `start_date` / `end_date` 区间 | 每个年度区间的 `end_date`。 |
| `by_trade_date` | 按交易日逐日拉全市场 | `trade_date`。 |
| `by_calendar_date` | 按自然日逐日拉 | 对应自然日。 |
| `by_period` | 按季度报告期拉 | 季度末 `period` 或 `enddate`。 |

当前 endpoint 的分区口径：

| endpoint | api | mode | date_param | raw `partition_date` |
|---|---|---|---|---|
| `trade_cal` | `trade_cal` | `date_range` | `start_date/end_date` | 请求区间 `end_date` |
| `daily` | `daily` | `by_trade_date` | `trade_date` | 交易日 |
| `adj_factor` | `adj_factor` | `by_trade_date` | `trade_date` | 交易日 |
| `dividend` | `dividend` | `by_trade_date` | `ex_date` | 除权除息日 |
| `stock_basic_listed` | `stock_basic` | `snapshot` | 无 | 快照拉取日 |
| `stock_basic_delisted` | `stock_basic` | `snapshot` | 无 | 快照拉取日 |
| `stock_basic_pending` | `stock_basic` | `snapshot` | 无 | 快照拉取日 |
| `daily_basic` | `daily_basic` | `by_trade_date` | `trade_date` | 交易日 |
| `stk_limit` | `stk_limit` | `by_trade_date` + 分页 | `trade_date` | 交易日 |
| `suspend_d` | `suspend_d` | `by_trade_date` | `trade_date` | 停复牌日期 |
| `namechange` | `namechange` | `by_year_range` | `start_date/end_date` | 年度请求区间 `end_date` |
| `moneyflow` | `moneyflow` | `by_trade_date` + 分页 | `trade_date` | 交易日 |
| `margin` | `margin` | `by_trade_date` | `trade_date` | 交易日 |
| `margin_detail` | `margin_detail` | `by_trade_date` + 分页 | `trade_date` | 交易日 |
| `income` | `income_vip` | `by_period` | `period` | 报告期 |
| `balancesheet` | `balancesheet_vip` | `by_period` | `period` | 报告期 |
| `cashflow` | `cashflow_vip` | `by_period` | `period` | 报告期 |
| `fina_indicator` | `fina_indicator_vip` | `by_period` | `period` | 报告期 |
| `top10_holders` | `top10_holders` | `by_period` | `period` | 报告期 |
| `top10_floatholders` | `top10_floatholders` | `by_period` | `period` | 报告期 |
| `stk_holdernumber` | `stk_holdernumber` | `by_period` | `enddate` | 期末日 |
| `forecast` | `forecast_vip` | `by_period` | `period` | 报告期 |
| `express` | `express_vip` | `by_period` | `period` | 报告期 |

注意：财务和业绩类 raw 文件按报告期分区只是为了回补组织方便，不代表市场在报告期当天可见。后续做 PIT 特征时必须使用 `ann_date` / `f_ann_date` 等公告日期。

## 9. 优先级

优先级是当前拉取顺序，不是写死在 Python 里的固定分类。后续新增 endpoint 时，应通过 YAML 追加 priority 和 endpoint，不修改调度代码。

```yaml
priority_order:
  - "p0"
  - "p1"
  - "p2"
```

### p0：先解决前复权未来信息风险

| endpoint | 目的 |
|---|---|
| `trade_cal` | 交易日列表，供任务拆分和后续时间对齐。 |
| `daily` | 不复权日线行情。 |
| `adj_factor` | 复权因子，用于后续按 as-of 构造复权序列。 |
| `dividend` | 分红送股除权信息，用于审计复权变化。 |

### p1：其余第一阶段日线核心数据

| endpoint | 目的 |
|---|---|
| `stock_basic_listed` / `delisted` / `pending` | 股票池、上市状态、生存者偏差控制。 |
| `daily_basic` | 市值、估值、换手、成交额等日频指标。 |
| `stk_limit` | 涨跌停价格，可交易性约束。 |
| `suspend_d` | 停牌信息，可交易性约束。 |
| `namechange` | 曾用名、ST 等名称历史辅助信息。 |

### p2：第二阶段增强数据

| endpoint | 目的 |
|---|---|
| `moneyflow` | 个股资金流。 |
| `margin` / `margin_detail` | 融资融券。 |
| `income_vip` / `balancesheet_vip` / `cashflow_vip` / `fina_indicator_vip` | 财务三表和财务指标，按季度全市场 VIP 拉取。 |
| `top10_holders` / `top10_floatholders` | 十大股东和十大流通股东。 |
| `stk_holdernumber` | 股东户数。 |
| `forecast_vip` / `express_vip` | 业绩预告和业绩快报，按季度全市场 VIP 拉取。 |

## 10. 权限和限频

当前执行假设：

| 项 | 当前口径 |
|---|---|
| Tushare 积分计划 | 用户计划购买 5000 积分。 |
| 代码执行限频 | `120 次/分钟`。 |
| 财务类接口 | 使用 5000 积分可用的 `_vip` 季度全市场接口。 |
| 分钟线/集合竞价/新闻全文 | 当前不接入，很多属于独立权限或后续阶段。 |

已采用 VIP 的 p2 接口：

- `income_vip`
- `balancesheet_vip`
- `cashflow_vip`
- `fina_indicator_vip`
- `forecast_vip`
- `express_vip`

如果购买积分后后台实际频次高于 `120 次/分钟`，再调整配置：

```yaml
tushare:
  max_calls_per_minute: 120
```

## 11. 行数限制和分页

Tushare 部分接口有单次返回行数限制。第一版必须避免静默截断。

要求：

1. endpoint 可配置 `row_limit`。
2. 非分页 endpoint 如果返回行数达到 `row_limit`，默认报错。
3. 可分页 endpoint 使用 `limit` / `offset` 拉取。
4. 可分页 endpoint 可配置 `page_size` 和 `max_pages`。
5. 如果刚好达到 `max_pages * page_size`，额外 probe 下一页；下一页为空则允许成功。
6. 如果达到 `max_pages` 后下一页仍有数据，报错并要求拆分得更细。
7. 如果分页返回重复页，认为该 endpoint 可能不支持 `limit` / `offset`，报错并要求改为更细粒度拆分。

示例：

```yaml
income:
  api_name: "income_vip"
  mode: "by_period"
  row_limit: 6000
  paginate: true
  page_size: 5000
  max_pages: 20
```

说明：`limit` / `offset` 是通用分页策略；如果实际 smoke test 发现某个 Tushare endpoint 不支持该参数，则改为更细粒度拆分，例如按公告日、股票代码或其他参数拆分。

当前配置中 `stk_limit`、`moneyflow` 和 `margin_detail` 使用 `page_size=5000`、`max_pages=3` 分页拉取，避免全市场日频接口接近或超过单页上限时出现截断风险。`namechange` 官方要求至少传入 `ts_code`、`start_date/end_date` 或 `ann_date` 之一，因此不能用空参数 snapshot，当前用 `by_year_range` 按年切分历史区间。

## 12. 断点续传

断点续传基于 GCS `_checkpoints`。

checkpoint 路径：

```text
_checkpoints/endpoint=<endpoint_key>/logical_date=<YYYYMMDD>.json
```

默认策略：

```yaml
checkpoint:
  enabled: true
  prefix: "_checkpoints"
  skip_statuses: ["uploaded"]
```

行为：

- 成功写入 raw 后写 checkpoint。
- 下次运行如果发现同一 endpoint + logical_date 已是 `uploaded`，则跳过。
- `empty` 默认不跳过，避免“当天数据暂未更新”被永久记为空。
- 失败任务写 manifest，不写成功 checkpoint，下次会继续尝试。
- 单个任务失败不终止后续任务；整批结束后写 summary。
- 默认 `run_behavior.fail_run_on_job_error=true`，只要本轮存在 failed record，CLI 最终仍返回失败状态，便于 Cloud Run/Scheduler 告警。

## 13. Manifest

每个任务写一条 manifest record，整次运行写一个 summary。

record 至少包含：

| 字段 | 说明 |
|---|---|
| `run_id` | 本次运行 ID。 |
| `endpoint_key` | endpoint。 |
| `api_name` | Tushare API。 |
| `target_table` | 目标表元数据。 |
| `priority` | p0/p1/p2。 |
| `params` | 请求参数，不含 token。 |
| `logical_date` | 任务逻辑日期。 |
| `partition_month` | 标准层分区月或 `all`。 |
| `rows` | 返回行数。 |
| `status` | `uploaded` / `empty` / `failed` / `skipped`。 |
| `raw_uri` | raw GCS URI。 |
| `standardized_uri` | 如果开启标准层，则记录 URI。 |
| `ingested_at` | 任务执行时间。 |
| `error` | 失败信息。 |

## 14. 运行方式

本地计划任务：

```bash
python -m data_ingestion.tushare_to_gcs plan \
  --config config/tushare_to_gcs.yaml \
  --priority-through p2 \
  --start-date 20190101 \
  --end-date 20260525
```

本地 smoke test：

```bash
ASHARE_USE_GCLOUD_ACCESS_TOKEN=1 TUSHARE_TOKEN=... \
python -m data_ingestion.tushare_to_gcs run \
  --config config/tushare_to_gcs.yaml \
  --endpoint daily \
  --start-date 20260522 \
  --end-date 20260522 \
  --max-calls 1
```

Cloud Run Job 建议分阶段运行：

```bash
PRIORITY=p0 START_DATE=20190101 END_DATE=20260525 \
  ./deploy/cloud_run_tushare_ingest/run.sh

PRIORITY=p1 START_DATE=20190101 END_DATE=20260525 \
  ./deploy/cloud_run_tushare_ingest/run.sh

PRIORITY=p2 START_DATE=20190101 END_DATE=20260525 \
  ./deploy/cloud_run_tushare_ingest/run.sh
```

不建议第一版直接一次性 `PRIORITY_THROUGH=p2` 全跑，分段运行更利于观察、失败重试和成本定位。

## 15. 预计耗时

范围：`20190101` 到 `20260525`。

基础计数：

| 口径 | 数量 |
|---|---:|
| 自然日 | 2702 |
| 交易日 | 约 1790 |
| 季度报告期 | 29 |

按当前 `120 次/分钟`：

| 阶段 | 估算请求数 | 理论耗时 | 建议预留 |
|---|---:|---:|---:|
| p0 | 约 5370 | 约 45 分钟 | 55-70 分钟 |
| p1 | 约 7170 | 约 60 分钟 | 75-95 分钟 |
| p2 | 约 7400-10600 | 约 62-89 分钟 | 90-130 分钟 |
| 合计 | 约 19945-23145 | 约 2.8-3.2 小时 | 3.7-5.0 小时 |

p2 不确定性最大，原因是财务、股东和业绩类接口可能分页。

## 16. 验收标准

必须满足：

1. 能按 `--priority p0/p1/p2` 分段生成 request plan。
2. 能按 `--priority-through p2` 按配置顺序生成完整计划。
3. 能对单个 endpoint 做 smoke test 并写入 GCS。
4. raw Parquet 包含原始 Tushare 字段和 ingestion metadata。
5. `_manifests` 能记录每个任务状态、行数、参数和 raw URI。
6. `_checkpoints` 能让已上传任务在下次运行时跳过。
7. 非分页接口命中 `row_limit` 时不允许静默成功。
8. p2 财务和业绩类使用 `_vip` 接口。
9. 代码中不包含 MaxCompute / ODPS 依赖。
10. Tushare token 不写入仓库、不打印到日志。

## 17. 后续 PRD

本 PRD 只覆盖 Tushare 到 GCS。后续内容已经拆分为：

| PRD | 范围 |
|---|---|
| `PRD_20260525_07_Tushare_GCS到BigQuery分层.md` | GCS raw 到 BigQuery ODS/DWD/DWS/ADS、PIT、数据质量和成本控制。 |
| `PRD_20260525_08_Tushare模型重训与策略接入.md` | 训练窗口、标签、特征、模型 registry、样本外回测和旧模型隔离。 |
| `PRD_20260525_09_Tushare生产调度与成本监控.md` | Cloud Run 调度、每日增量、月度重训、失败告警和成本监控。 |
