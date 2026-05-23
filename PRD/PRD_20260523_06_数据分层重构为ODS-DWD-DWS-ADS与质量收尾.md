# 数据分层重构为 ODS-DWD-DWS-ADS 与质量收尾 PRD

## 1. 元信息

| 字段 | 内容 |
| --- | --- |
| LLM 型号 | Claude Opus 4.7 |
| 输出时间戳 | 2026-05-24 01:20:00 |
| 文档编号 | PRD_20260523_06 |
| 关联 Commit | 09ae287；本次按用户决策修订 |
| 需求优先级 | P0 |
| 文档定位 | 路线图总览 / 索引页；实施由 PRD_07~12 分别承接 |

## 2. 背景与动机

当前已完成一版 GCS Parquet 与 BigQuery staging 装载：

- GCS 输入源：`gs://data-aquarium/a-share/standardized_parquet/`
- GCS Parquet 文件数：`13744`
- 旧 BigQuery dataset：`ashare_raw` / `ashare_core` / `ashare_mart`
- 旧 staging 表：`ashare_raw.stg_*`

用户已确认后续不再生成新版 GCS 数据。现有 GCS prefix 是后续 ODS 外部表的正式输入源，不是待删除的旧备份。

为降低个人量化项目的运维复杂度，本系列 PRD 不采用 4 个物理 dataset，而采用 **1 个 BigQuery dataset + 表名前缀**：

- dataset：`ashare`
- ODS 表：`ashare.ods_*`
- DWD 表：`ashare.dwd_*`
- DWS 表：`ashare.dws_*`
- ADS 表：`ashare.ads_*`

这样保留 ODS/DWD/DWS/ADS 的逻辑分层语义，同时避免个人项目不需要的权限边界、跨 dataset JOIN 和后续迁移成本。

## 3. 影响模块声明

| 模块 | 是否影响 | 说明 |
| --- | --- | --- |
| `data_layer` | 是 | `BigQueryDataSource` 后续默认读取 `ashare` dataset 下的 `dwd_*` 表 |
| `engine` | 否 | 不修改撮合、事件推进和回测循环 |
| `account` | 否 | 不修改资金、持仓和成交逻辑 |
| `strategy` | 否 | 不修改已有策略信号 |
| `analytics` | 否 | 不修改绩效计算 |
| `config` | 是 | `config/backtest.yaml` 和 `gcs_to_bigquery/config.yaml` 需要从旧 dataset 语义切到 `ashare` + 表前缀 |
| `utils` | 否 | 不修改通用工具 |
| `CLI` | 是 | `gcs_to_bigquery/pipeline.py` 需要支持 `create-ods-external` / `audit-ods` / `transform-dwd` 等命令 |

是否影响回测结果可复现性：是。PRD_12 完成前，BigQuery 回测处于不可用的过渡状态；PRD_12 必须证明相同参数、相同数据版本下结果可重复。

## 4. 关键文件路径与现有函数签名

```python
# data_layer/bigquery_source.py
class BigQueryDataSource(BaseDataSource):
    def __init__(
        self,
        project_id: str,
        dataset: str = "ashare_core",
        location: str = "asia-east2",
        ...
    ) -> None:
        ...
```

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

```python
# gcs_to_bigquery/pipeline.py
def load(config: dict, dry_run: bool) -> None:
    manifest_path = norm_path(config["manifest_path"])
    records = read_manifest(manifest_path)
    if not records:
        records = list(iter_gcs_records(config))
        write_manifest(manifest_path, records)
```

## 5. 需求详情

### 5.1 功能目标

本系列拆为 6 个子 PRD：

| 编号 | 名称 | 定位 | 依赖 |
| --- | --- | --- | --- |
| PRD_20260523_07 | BigQuery 数据分层命名迁移 | 单 dataset `ashare` + 表前缀命名 | 无 |
| PRD_20260523_08 | 装载管道运维健壮性收尾 | 认证、路径、超时、一次性脚本和配置清理 | 无 |
| PRD_20260523_09 | 字段映射与代码规则修复 | 维护现有 GCS schema 到 DWD 标准字段的映射规则；不重生成 Parquet | 无 |
| PRD_20260523_10 | ODS 外部表与审计改造 | ODS external table、manifest、errors、audit | 07 / 08 / 09 可并行 |
| PRD_20260523_11 | 现有 GCS 数据创建 ODS 外部表 | 复用当前 `standardized_parquet/` 创建 `ashare.ods_*` external table | 07 / 08 / 10 |
| PRD_20260523_12 | DWD 转换与 BigQueryDataSource 适配 | `ashare.ods_*` → `ashare.dwd_*`，回测读取验收 | 11 |

### 5.2 交互流程

```text
现有 GCS Parquet
  -> ashare.ods_*（贴源层，保留现有 Parquet schema 和元数据）
  -> ashare.dwd_*（标准字段、严格类型、主键去重、分区聚簇）
  -> ashare.dws_* / ashare.ads_*（后续特征、信号、应用层）
  -> BigQueryDataSource 读取 ashare.dwd_* 表
```

过渡期决策：

- PRD_07 完成后允许 `config/backtest.yaml` / `BigQueryDataSource` 指向 `ashare`。
- PRD_12 完成前，BigQuery 回测可以不可用，不提供自动 fallback 到旧 `ashare_core`。
- 旧 BigQuery dataset 不作为 rollback 资产；新 ODS/DWD 验收通过后按成本收尾删除。
- 当前 GCS prefix `standardized_parquet/` 是正式输入源，必须保留。

## 6. 配置变更

| 配置项 | 旧值 | 新值 |
| --- | --- | --- |
| `config/backtest.yaml:data.bigquery.dataset` | `ashare_core` | `ashare` |
| `config/backtest.yaml:data.bigquery.tables.kline_1d_equity` | `fact_equity_kline_1d` | `dwd_fact_equity_kline_1d` |
| `config/backtest.yaml:data.bigquery.tables.kline_1d_fund` | `fact_fund_kline_1d` | `dwd_fact_fund_kline_1d` |
| `config/backtest.yaml:data.bigquery.tables.kline_1d_index` | `fact_index_kline_1d` | `dwd_fact_index_kline_1d` |
| `config/backtest.yaml:data.bigquery.tables.dim_security` | `dim_security` | `dwd_dim_security` |
| `gcs_to_bigquery/config.yaml:gcs.prefix` | `a-share/standardized_parquet` | 保持不变 |
| `gcs_to_bigquery/config.yaml:datasets.*` | `raw/core/mart` | 单 dataset：`ashare`；层级由表前缀表达 |

## 7. 不可改动的红线区域

- 不删除 `gs://data-aquarium/a-share/standardized_parquet/`；它是唯一正式 GCS 输入源。
- 不再要求生成 `standardized_parquet_v2/` 或其他新版 GCS prefix。
- 不修改 `engine/`、`account/`、`strategy/` 的交易和策略逻辑。
- 不修改 `BaseDataSource.get_bars()` 对外接口和返回列语义。
- 不把未通过 `audit-ods` / `audit-dwd` 的表接入回测。
- DWD/策略读取层的股票事实表字段采用 `equity_code`；ODS 允许保留现有 GCS Parquet 的贴源字段；`dim_security` 作为多资产维表继续采用 `security_code`。
- 基金、指数、板块事实表分别采用 `fund_code`、`index_code`、`board_code`。
- 财务表不得使用 `report_period` 作为防未来函数场景下的业务可见日期。

## 8. 修改范围与位置

| PRD | 主要修改范围 | 明确不做 |
| --- | --- | --- |
| PRD_07 | dataset 与表名前缀命名；配置与架构文档 | 不装载数据 |
| PRD_08 | 运维健壮性与配置清理 | 不删除支持现有中文字段装载所需的兼容配置 |
| PRD_09 | 字段映射、资产代码字段命名、北交所规则、财务日期规则 | 不重生成 Parquet |
| PRD_10 | ODS external table、manifest、errors、audit | 不切换回测 |
| PRD_11 | 从当前 GCS prefix 创建 ODS 外部表并审计 | 不启动 GCE 重建 Parquet |
| PRD_12 | DWD transform、BigQueryDataSource 读取、回测可复现验收 | 不依赖旧 core 作为 rollback |

## 9. 验收标准

- PRD_07~10 的代码、配置、文档验收全部通过。
- PRD_11 使用 `gs://data-aquarium/a-share/standardized_parquet/` 创建 `ashare.ods_*` external table，文件数和 manifest 对账通过。
- PRD_12 生成 `ashare.dwd_*` P0 表，并通过字段、类型、主键、空值、分区和 smoke-query 验收。
- `BigQueryDataSource(project_id='data-aquarium')` 默认 dataset 为 `ashare`，实际表名指向 `dwd_*`。
- `double_ma + 510300.SH + 20240101~20240331 + capital=100000` 在 DWD 数据下重复运行两次，交易次数、最终资产、最大回撤完全一致。
- 旧 `ashare_raw` / `ashare_core` / `ashare_mart` 删除前，必须满足新 ODS/DWD audit 和 smoke-query 全部通过；删除动作仍需用户明确确认。
- `standardized_parquet/` 不参与删除验收，除非未来用户明确确认已有替代输入源。

### 测试用例输入输出

**用例 1：dataset 默认值**

- 输入：`BigQueryDataSource(project_id='data-aquarium').dataset`
- 修改前输出：`ashare_core`
- 修改后输出：`ashare`

**用例 2：股票事实表字段命名**

- 输入：DWD 股票日 K 表 schema
- 修改前字段：`security_code`
- 修改后预期字段：`equity_code`

**用例 3：GCS 输入源**

- 输入：`gcs_to_bigquery/config.yaml:gcs.prefix`
- 预期输出：`a-share/standardized_parquet`
- 禁止输出：`a-share/standardized_parquet_v2`

### 回测验证要求

- 策略：`double_ma`
- 标的：`510300.SH`
- 区间：`20240101` 至 `20240331`
- 初始资金：`100000`
- 数据源：`bigquery`
- 预期：DWD 数据下重复运行两次结果完全一致；若失败，不允许声明 PRD_12 完成。

## 10. 备注

- 本文档是路线图总览，不直接作为单一实施任务。
- PRD_20260523_05 保留为历史文档，由 PRD_12 取代其 core transform 角色。
- PRD_20260522_02 保留为历史文档，修订说明需指向本系列新决策。
