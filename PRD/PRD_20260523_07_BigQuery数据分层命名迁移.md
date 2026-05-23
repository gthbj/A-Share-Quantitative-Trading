# BigQuery 单 Dataset 分层命名迁移 PRD

## 1. 元信息

| 字段 | 内容 |
| --- | --- |
| LLM 型号 | Claude Opus 4.7 |
| 输出时间戳 | 2026-05-24 01:25:00 |
| 文档编号 | PRD_20260523_07 |
| 关联 Commit | 09ae287；本次按用户决策修订 |
| 需求优先级 | P0 |
| 所属拆分 | PRD_20260523_06 拆分子项 1/6 |
| 依赖 | 无 |

## 2. 背景与动机

旧 BigQuery 命名采用 3 个 dataset：

```text
ashare_raw
ashare_core
ashare_mart
```

用户已确认不采用 4 个物理 dataset 的 ODS/DWD/DWS/ADS 方案，而采用 **1 个 dataset + 表前缀**：

```text
data-aquarium.ashare.ods_*
data-aquarium.ashare.dwd_*
data-aquarium.ashare.dws_*
data-aquarium.ashare.ads_*
```

不做本迁移会导致后续 PRD 在 `ashare_ods` / `ashare_dwd` / `ashare_core` 等多个命名体系之间摇摆，增加 SQL、配置和文档维护成本。

## 3. 影响模块声明

| 模块 | 是否影响 | 说明 |
| --- | --- | --- |
| `data_layer` | 是 | 默认 dataset 切为 `ashare`，表名映射切为 `dwd_*` |
| `engine` / `account` / `strategy` / `analytics` / `utils` | 否 | 不修改 |
| `config` | 是 | `config/backtest.yaml`、`gcs_to_bigquery/config.yaml` |
| `CLI` | 是 | `gcs_to_bigquery/pipeline.py` 需要按单 dataset + layer prefix 生成表名 |

是否影响回测结果可复现性：过渡期影响可用性，不影响最终可复现性。PRD_12 完成前，BigQuery 回测允许不可用；不提供旧 `ashare_core` fallback。

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
# config/backtest.yaml
data:
  bigquery:
    dataset: "ashare_core"
    tables:
      kline_1d_equity: "fact_equity_kline_1d"
      kline_1d_fund: "fact_fund_kline_1d"
      kline_1d_index: "fact_index_kline_1d"
      dim_security: "dim_security"
```

```yaml
# gcs_to_bigquery/config.yaml
datasets:
  raw: "ashare_raw"
  core: "ashare_core"
  mart: "ashare_mart"
```

## 5. 需求详情

### 5.1 功能目标

- 创建或确认 BigQuery dataset：`data-aquarium.ashare`，location 为 `asia-east2`。
- 不创建 `ashare_ods` / `ashare_dwd` / `ashare_dws` / `ashare_ads` 四个 dataset。
- 用表名前缀表达分层：
  - `ods_*`
  - `dwd_*`
  - `dws_*`
  - `ads_*`
- `BigQueryDataSource` 默认 dataset 改为 `ashare`。
- `config/backtest.yaml` 中的表名指向 `dwd_*` 表。
- `gcs_to_bigquery/config.yaml` 不再使用 `datasets.raw/core/mart/ods/dwd/dws/ads` 多 dataset 键。
- `gcs_to_bigquery/pipeline.py` 中表名拼接应能表达 `ashare.ods_<target_table>` 与 `ashare.dwd_<target_table>`。
- 旧 dataset `ashare_raw` / `ashare_core` / `ashare_mart` 不作为 rollback；新链路验收通过后按用户确认删除。

### 5.2 交互流程

```text
配置读取 data.bigquery.dataset = ashare
  -> BigQueryDataSource 根据 tables.* 读取 dwd_* 表
  -> pipeline create-ods-external 创建 ashare.ods_* 外部表
  -> pipeline transform-dwd 写入 ashare.dwd_* 表
```

过渡期：

```text
PRD_07 完成
  -> 默认配置已指向 ashare / dwd_*
  -> PRD_11/12 未完成前，BigQuery 回测允许失败
  -> 不通过旧 ashare_core 自动 fallback
```

## 6. 配置变更

### 6.1 `config/backtest.yaml`

| 配置项 | 旧值 | 新值 |
| --- | --- | --- |
| `data.bigquery.dataset` | `ashare_core` | `ashare` |
| `data.bigquery.tables.kline_1d_equity` | `fact_equity_kline_1d` | `dwd_fact_equity_kline_1d` |
| `data.bigquery.tables.kline_1d_fund` | `fact_fund_kline_1d` | `dwd_fact_fund_kline_1d` |
| `data.bigquery.tables.kline_1d_index` | `fact_index_kline_1d` | `dwd_fact_index_kline_1d` |
| `data.bigquery.tables.dim_security` | `dim_security` | `dwd_dim_security` |
| `data.bigquery.tables.board_component` | `fact_board_component_1d` | `dwd_fact_board_component_1d` |

### 6.2 `gcs_to_bigquery/config.yaml`

```yaml
dataset: "ashare"
table_prefixes:
  ods: "ods_"
  dwd: "dwd_"
  dws: "dws_"
  ads: "ads_"
```

旧 `datasets.raw/core/mart` 删除。不得新增 `datasets.ods/dwd/dws/ads` 四 dataset 配置。

## 7. 不可改动的红线区域

- 不创建 4 个物理 dataset。
- 不删除 `gs://data-aquarium/a-share/standardized_parquet/`。
- 不在本 PRD 中装载 ODS 或生成 DWD。
- 不修改 `BaseDataSource` 接口签名。
- 不修改 `engine/`、`account/`、`strategy/`、`analytics/`、`utils/`。
- 不实现旧 `ashare_core` 自动 fallback。
- 不把旧 dataset 作为长期 rollback 资产写入文档。

## 8. 修改范围与位置

| 文件 | 修改位置 | 修改内容 |
| --- | --- | --- |
| `config/backtest.yaml` | `data.bigquery` | dataset 改 `ashare`；表名改 `dwd_*` |
| `data_layer/bigquery_source.py` | `__init__` 默认参数 | `dataset: str = "ashare"` |
| `gcs_to_bigquery/config.yaml` | dataset 配置 | 单 dataset + table_prefixes |
| `gcs_to_bigquery/pipeline.py` | table id 生成逻辑 | 使用 `ashare.<prefix><target_table>` |
| `gcs_to_bigquery/README.md` | 命名说明 | 同步单 dataset 分层 |
| `ARCHITECTURE.md` | BigQuery 数据状态 | 同步 `ashare.ods_*` / `ashare.dwd_*` |

不修改：`data_transfer/*`、`engine/*`、`account/*`、`strategy/*`、旧 GCS 数据。

## 9. 验收标准

- `bq ls --project_id=data-aquarium` 输出包含 `ashare`。
- 新配置中不出现 `ashare_ods`、`ashare_dwd`、`ashare_dws`、`ashare_ads`。
- 非历史文档和非迁移说明中不再把 `ashare_raw/core/mart` 描述为新链路目标。
- `BigQueryDataSource(project_id='data-aquarium').dataset == "ashare"`。
- `config/backtest.yaml` 表名映射全部为 `dwd_*`。
- `gcs_to_bigquery/config.yaml` 中 `gcs.prefix` 仍为 `a-share/standardized_parquet`。
- PRD_12 完成前运行 BigQuery 回测，如果 `dwd_*` 表未建成，允许明确失败，不允许静默切回旧 dataset。

### 测试用例输入输出

**用例 1：默认 dataset**

- 输入：`BigQueryDataSource(project_id='data-aquarium').dataset`
- 修改前输出：`ashare_core`
- 修改后输出：`ashare`

**用例 2：表名映射**

- 输入：读取 `config/backtest.yaml:data.bigquery.tables.kline_1d_equity`
- 修改前输出：`fact_equity_kline_1d`
- 修改后输出：`dwd_fact_equity_kline_1d`

**用例 3：ODS 表 ID**

- 输入：`target_table=fact_equity_kline_1d`、layer=`ods`
- 修改后预期 BigQuery 表：`data-aquarium.ashare.ods_fact_equity_kline_1d`

### 回测验证要求

本 PRD 不要求回测成功。PRD_12 前的回测不可用是已接受过渡状态；PRD_12 负责最终回测可复现验收。

## 10. 备注

- dataset 名选 `ashare`，不是 `equity` 或 `security`，因为本项目还包含基金、指数、板块和财务数据。
- `security_code` 作为维表多资产统称保留；股票事实表字段在 PRD_09/12 中改为 `equity_code`。
