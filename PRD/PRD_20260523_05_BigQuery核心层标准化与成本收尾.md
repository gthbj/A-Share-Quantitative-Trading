# BigQuery 核心层标准化与成本收尾 PRD

> **⚠️ 取代说明（2026-05-24 追加）**
>
> 本 PRD 由 Codex 在 2026-05-23 输出，设想从 `ashare_raw.stg_*`（中文 STRING 字段）→ `ashare_core.fact_*`（英文严格类型）做 SQL transform。
>
> 后续讨论中决定：
>
> 1. **数据分层重命名**为单 dataset `ashare` + 表前缀 `ods_ / dwd_ / dws_ / ads_`（详见 PRD_20260523_07）
> 2. **不重生成 GCS Parquet**，只使用当前 `gs://data-aquarium/a-share/standardized_parquet/`
> 3. **ODS 改为 BigQuery external table over GCS Parquet**，不再复制一份 native ODS 业务表
> 4. **字段标准化放到 DWD transform 阶段**（详见 PRD_20260523_09 / PRD_20260523_12）——ODS 保留现有 GCS Parquet 的贴源 schema，DWD 生成标准英文严格类型表
>
> 因此本 PRD 被 **PRD_20260523_12** 取代。PRD_12 承担余下的工作：
>
> - ODS external table（贴源字段）→ DWD native table（英文严格类型）的 SQL transform
> - 主键去重、`SAFE_CAST` 类型转换
> - `BigQueryDataSource` SQL 模板适配 DWD
> - `transform-dwd / audit-dwd / smoke-query` 子命令
> - 回测一致性验证
>
> **本 PRD 不再实施**，保留为历史。新读者请直接看：
> - PRD_20260523_06（路线图总览）
> - PRD_20260523_12（DWD 转换适配，取代本 PRD）
>
> ---

## 1. 元信息

| 字段 | 内容 |
| --- | --- |
| LLM 型号 | GPT-5 Codex |
| 输出时间戳 | 2026-05-23 23:01:16 |
| 文档编号 | PRD_20260523_05 |
| 关联 Commit | c110601（初版 PRD）；本次修订为当前提交（按项目偏好修订 PRD 并同步架构状态） |
| 需求优先级 | P0 |

## 2. 背景与动机

本次 GCP 数据迁移已经完成 GCS Parquet 落地与 BigQuery staging 装载：

- GCS Parquet 前缀：`gs://data-aquarium/a-share/standardized_parquet/`
- GCS Parquet 文件数：`13744`
- BigQuery staging dataset：`ashare_raw`
- BigQuery staging 表：`ashare_raw.stg_*`
- BigQuery staging 表数：`36`
- 本地与 BigQuery manifest 状态：`13744/13744 loaded`
- GCE 构建 VM：`ashare-parquet-worker` 已停止，状态 `TERMINATED`

当前 staging 表仍保留源文件 schema。以日 K 表为例，`ashare_raw.stg_fact_equity_kline_1d` 中存在 `开盘`、`收盘`、`股票代码` 等源字段；策略侧 `data_layer.bigquery_source.BigQueryDataSource` 则期望读取 `ashare_core.fact_equity_kline_1d` 中的 `open`、`close`、`security_code`、`partition_month` 等标准字段。

如果不做核心层标准化，后果是：

- 回测无法稳定使用 BigQuery 数据源。
- 策略代码会因字段缺失或类型不匹配失败。
- staging 表长期保留会持续产生 BigQuery 存储费用。
- 已停止 VM 因保留 Local SSD 状态仍可能继续产生存储相关费用。
- 后续财务因子、行业暴露和机器学习策略无法基于统一 schema 开发。

本需求用于定义下一阶段的边界、验收标准和不可改动区域，避免在未确认字段映射时污染 `ashare_core`。

## 3. 影响模块声明

| 模块 | 是否影响 | 说明 |
| --- | --- | --- |
| `data_layer` | 是 | `BigQueryDataSource` 依赖 `ashare_core` 标准字段，需用 smoke test 验证读取行为 |
| `engine` | 否 | 不修改撮合、账户、回测推进逻辑 |
| `account` | 否 | 不修改资金与持仓逻辑 |
| `strategy` | 间接影响 | 数据可用性提升后，策略回测可从 BigQuery 读取真实数据 |
| `analytics` | 否 | 不修改绩效计算逻辑 |
| `config` | 是 | 可能新增 GCS-to-BigQuery core transform 配置、查询成本限制配置 |
| `utils` | 否 | 不修改交易日历、代码工具和日志工具 |
| `CLI` | 是 | 可能新增 `transform-core`、`audit-core`、`smoke-query` 命令 |

是否影响回测结果可复现性：是。核心层字段标准化会决定回测读取到的行情价格、成交量和股票列表。验收必须证明相同策略、参数、时间区间在同一 BigQuery snapshot 下结果可复现。

## 4. 关键文件路径与现有函数签名

### 4.1 BigQuery 数据源

```python
# data_layer/bigquery_source.py
def get_stock_list(self) -> pd.DataFrame:
    """从 dim_security 获取股票列表。

    返回列：[code, name, list_date, industry]
    其中 industry 字段当前为空字符串（dim_security 未提供该字段）。
    """
```

```python
# data_layer/bigquery_source.py
def _fetch_daily_bars(
    self,
    code: str,
    start_date: str,
    end_date: str,
    adjust: str,
) -> pd.DataFrame:
    """日K查询。根据代码类型自动路由到 equity / fund / index 表。"""
```

```python
# data_layer/bigquery_source.py
@staticmethod
def _partition_months_in_range(start_date: str, end_date: str) -> List[int]:
    """根据 [YYYYMMDD, YYYYMMDD] 返回覆盖的 partition_month 列表（INT，YYYYMM）。"""
```

### 4.2 GCS 到 BigQuery 装载管道

```python
# gcs_to_bigquery/pipeline.py
def load(config: dict, dry_run: bool) -> None:
    manifest_path = norm_path(config["manifest_path"])
    records = read_manifest(manifest_path)
    if not records:
        records = list(iter_gcs_records(config))
        write_manifest(manifest_path, records)
```

```python
# gcs_to_bigquery/pipeline.py
def load_table_batch(config: dict, client, target_table: str, records: list[LoadRecord]) -> list[LoadRecord]:
    bigquery = require_bigquery()
    destination = table_id(config, "raw", staging_table_name(target_table))
```

```python
# gcs_to_bigquery/pipeline.py
def audit_staging(config: dict) -> None:
    records = read_manifest(norm_path(config["manifest_path"]))
    loaded_tables = sorted({record.target_table for record in records if record.status == "loaded"})
```

### 4.3 配置文件

```yaml
# gcs_to_bigquery/config.yaml
project_id: "data-aquarium"
location: "asia-east2"
gcs:
  bucket: "data-aquarium"
  prefix: "a-share/standardized_parquet"
datasets:
  raw: "ashare_raw"
  core: "ashare_core"
  mart: "ashare_mart"
```

## 5. 需求详情

### 5.1 功能目标

本需求必须交付以下能力：

- 生成 P0 核心表：
  - `ashare_core.fact_equity_kline_1d`
  - `ashare_core.fact_fund_kline_1d`
  - `ashare_core.fact_index_kline_1d`
  - `ashare_core.dim_security`
  - `ashare_core.fact_board_component_1d`
- 为 P0 核心表建立显式字段映射，禁止 `SELECT *` 直接进入 core。
- 把源字段安全转换成策略侧需要的英文标准字段。
- 建立 core 层审计命令，覆盖行数、主键重复、关键字段空值、日期范围和分区一致性。
- 提供 BigQueryDataSource smoke test，证明策略读取路径可用。
- 明确 VM、Local SSD、boot disk、staging 表的成本收尾验收条件。

### 5.2 交互流程

研发/运维触发核心层转换：

```text
用户运行 core transform 命令
  -> 系统读取 gcs_to_bigquery/config.yaml
  -> 系统读取 ashare_raw.stg_* 表 schema
  -> 系统按显式字段映射生成 ashare_core.* 表
  -> 系统运行 audit-core
  -> audit 通过后允许 BigQueryDataSource smoke test
```

策略读取路径：

```text
run_backtest.py / strategy train
  -> BigQueryDataSource
  -> ashare_core.fact_* / dim_*
  -> partition_month IN (...) 分区裁剪
  -> 返回框架标准 DataFrame
```

成本收尾路径：

```text
确认 GCS audit 通过 + staging audit 通过 + PRD/代码已入库
  -> 用户明确同意删除 VM
  -> 删除 ashare-parquet-worker
  -> 确认 boot disk 和 Local SSD preserved state 不再继续计费
```

## 6. 配置变更

可能新增或确认以下配置项：

| 配置项 | 类型 | 默认值 | 作用 |
| --- | --- | --- | --- |
| `defaults.core_write_mode` | STRING | `replace` | core 表生成方式，第一版使用 `CREATE OR REPLACE TABLE` |
| `defaults.max_query_bytes` | INT64 | 待定 | smoke query 或回测查询的最大允许扫描字节数 |
| `tables.<table>.core_enabled` | BOOL | `false` | 是否允许从 staging 生成对应 core 表 |
| `tables.<table>.primary_key` | LIST | 按表配置 | core 表主键与去重依据 |
| `tables.<table>.partition_field` | STRING | `partition_month` | BigQuery 分区字段 |
| `tables.<table>.clustering_fields` | LIST | 按表配置 | BigQuery 聚簇字段 |

不得修改 `config/backtest.yaml` 中现有 BigQuery 表名键的含义；若需要增加查询成本限制，应新增配置项，不复用无关字段。

## 7. 不可改动的红线区域

- 不修改 `engine/`、`account/` 的撮合、资金、持仓逻辑。
- 不修改 `strategy/` 中已有策略的信号逻辑。
- 不修改 `BaseDataSource.get_bars()` 对外接口。
- 不修改 `BigQueryDataSource.get_bars()` 的返回列语义：`code, date, open, high, low, close, volume, amount`。
- 不把 staging 表直接 `SELECT *` 写入 `ashare_core`。
- 不在未通过 audit 的情况下启用 `ashare_core` 给回测使用。
- 不自动删除 VM；删除 `ashare-parquet-worker` 必须得到用户明确确认。
- 不删除 GCS `a-share/standardized_parquet/` 数据。
- 不破坏回测可复现性：相同策略、参数、数据版本，多次运行结果必须一致。

## 8. 修改范围与位置

### 8.1 主要修改文件

| 文件 | 修改位置 | 修改内容 |
| --- | --- | --- |
| `gcs_to_bigquery/pipeline.py` | CLI 子命令区域 | 增加 `transform-core`、`audit-core`、`smoke-query` |
| `gcs_to_bigquery/pipeline.py` 或新增 `gcs_to_bigquery/core_transform.py` | core 转换逻辑 | 按表执行显式字段映射和类型转换 |
| `gcs_to_bigquery/config.yaml` | `tables` 配置 | 增加 core_enabled、primary_key、partition/clustering 配置 |
| `data_layer/bigquery_source.py` | smoke 验证，不优先改接口 | 如 core 字段与当前 SQL 不一致，只允许做最小字段适配 |
| `ARCHITECTURE.md` | BigQuery 数据状态章节 | 更新 staging/core 当前状态、验收边界 |
| `gcs_to_bigquery/README.md` | 操作文档 | 补充 core transform、audit、成本收尾说明 |

### 8.2 不修改的文件

- `engine/backtest.py`
- `engine/trade_engine.py`
- `account/portfolio.py`
- `account/position.py`
- `strategy/*/strategy.py`
- `analytics/*`
- `utils/calendar.py`
- `utils/code.py`

## 9. 验收标准

### 9.1 核心表验收

- `ashare_core.fact_equity_kline_1d` 创建成功，字段至少包含：
  - `date`
  - `partition_month`
  - `security_code`
  - `adjust_type`
  - `open`
  - `high`
  - `low`
  - `close`
  - `volume`
  - `amount`
  - `source_file`
  - `source_entry`
  - `ingested_at`
- `ashare_core.fact_fund_kline_1d` 创建成功，字段至少包含 `fund_code, date, open, high, low, close, volume, amount, partition_month`。
- `ashare_core.fact_index_kline_1d` 创建成功，字段至少包含 `index_code, date, open, high, low, close, volume, amount, partition_month`。
- `ashare_core.dim_security` 创建成功，字段至少包含 `security_code, security_name, security_type, exchange, list_date, delist_date, is_active`。
- `ashare_core.fact_board_component_1d` 创建成功，字段至少包含 `date, board_code, security_code, partition_month`。

### 9.2 数据质量验收

- 主键重复数为 0：
  - `fact_equity_kline_1d`: `security_code, date, adjust_type`
  - `fact_fund_kline_1d`: `fund_code, date, adjust_type`
  - `fact_index_kline_1d`: `index_code, date`
  - `dim_security`: `security_code`
- `date`、代码字段、`close` 的空值率满足验收阈值；P0 行情表关键字段空值率不得超过 0.1%。
- `partition_month` 与 `date` 一致：
  - `partition_month = EXTRACT(YEAR FROM date) * 100 + EXTRACT(MONTH FROM date)`
- core 表 row count 不得超过 staging 原表 row count。
- 所有 core 转换 SQL 必须显式列字段。

### 9.3 测试用例预期输入输出

用例 1：股票日 K 字段标准化

- 输入：`ashare_raw.stg_fact_equity_kline_1d` 中一行，字段包含 `股票代码 = 000001.SZ`、`日期 = 2024-01-02`、`开盘 = 9.80`、`收盘 = 10.10`。
- 修改前行为：`BigQueryDataSource` 无法从 `ashare_core.fact_equity_kline_1d` 稳定读取该行。
- 修改后预期输出：`ashare_core.fact_equity_kline_1d` 中存在 `security_code = 000001.SZ`、`date = 2024-01-02`、`open = 9.80`、`close = 10.10`、`partition_month = 202401`。

用例 2：指数日 K 字段标准化

- 输入：`ashare_raw.stg_fact_index_kline_1d` 中一行，字段包含 `代码 = 000300.SH`、`日期 = 2024-01-02`、`收盘 = 3500.00`。
- 修改后预期输出：`ashare_core.fact_index_kline_1d` 中存在 `index_code = 000300.SH`、`date = 2024-01-02`、`close = 3500.00`。

用例 3：股票列表读取

- 输入：`ashare_core.dim_security` 中存在 `security_code = 000001.SZ`、`security_name = 平安银行`、`is_active = TRUE`。
- 修改后预期输出：`BigQueryDataSource.get_stock_list()` 返回行中包含 `code = 000001.SZ`、`name = 平安银行`。

### 9.4 回测验证要求

- 策略：`double_ma`
- 回测区间：`20240101` 至 `20240331`
- 标的：`510300.SH`
- 初始资金：`100000`
- 数据源：`bigquery`
- 预期行为：
  - 回测能完整运行并输出报告。
  - 日 K 查询必须命中 `partition_month IN (202401, 202402, 202403)`。
  - 同一数据版本下重复运行，交易次数、最终资产、最大回撤一致。

### 9.5 成本收尾验收

- 若用户确认删除 VM，则 `ashare-parquet-worker` 删除后不再存在。
- `gcloud compute disks list --filter="name~ashare-parquet-worker"` 不应出现遗留 boot disk，除非用户明确要求保留。
- staging 表保留策略明确，不能无限期保留所有临时表而无说明。
- BigQuery batch load 不应再次重复加载已 loaded 的 13744 个对象。

## 10. 备注

- 本阶段主要风险不是数据是否已经进入 BigQuery，而是源字段到 core 字段的语义映射是否正确。
- 财务表和板块表字段更复杂，不应和 P0 行情表混在同一次验收中强行完成。
- 回测验证必须避免 Lookahead Bias：只能读取回测当时已经可见的数据，不得使用未来公告或未来财报字段。
- BigQuery 查询必须做分区裁剪，避免全表扫描造成不必要费用。
- VM 删除属于资源破坏性操作，必须由用户明确确认后执行。
