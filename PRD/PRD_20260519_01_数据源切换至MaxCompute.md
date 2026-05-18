# 数据源切换至阿里云 MaxCompute

## 1. 元信息

| 项目 | 内容 |
|------|------|
| LLM型号 | Claude Opus 4.7 |
| 输出时间戳 | 2026-05-19 14:00:00 |
| 文档编号 | PRD_20260519_01 |
| 关联 Commit | 待生成 |
| 需求优先级 | P0（最高） |

---

## 2. 背景与动机

历史上本框架默认使用 AKShare（免费）作为主数据源、Tushare Pro 作为备选。在实际使用中暴露出以下问题：

- **AKShare 接口稳定性差**：东财 API 频繁触发反爬封禁、`Empty reply from server`，分钟级数据被部分屏蔽，框架不得不退化到"基于日K生成模拟分钟数据"。
- **Tushare Pro 积分门槛**：当前 Token 积分不足以拉取行情，无法作为可靠备选。
- **数据源不在自己控制下**：第三方服务的可用性、字段变更、限频策略都是不可控的外部依赖，回测复现性受影响。

项目所有者已在阿里云 MaxCompute 上自建权威历史行情库（项目名 `a_share_historical_data`），需将本框架默认数据源切换为 MaxCompute，使行情供给完全可控。

**不实现的后果**：
- 框架长期处于"AKShare 不稳定 / Tushare 无权限"的尴尬状态，回测随时可能因数据问题中断。
- 后续策略迭代缺乏稳定基线，无法保证多次回测结果一致。

**对回测行为的影响**：
- 回测结果可复现性显著提升（数据源稳定）。
- 由于 MaxCompute SQL 查询计费，首次拉取后需依赖本地 Parquet 缓存，并对缓存做时长/容量上限管理，避免长期累积撑爆磁盘或反复计费。

---

## 3. 影响模块声明

| 模块 | 影响程度 | 说明 |
|------|----------|------|
| `data_layer/maxcompute_source.py` | **新增** | 基于 pyodps 的 MaxCompute 数据源实现 |
| `data_layer/__init__.py` | 修改 | 导出 `MaxComputeDataSource` |
| `data_layer/akshare_source.py` | 保留不动 | 备选数据源，源码不删除，但不再被 `run_backtest.py` 装载 |
| `data_layer/tushare_source.py` | 保留不动 | 备选数据源，源码不删除，但不再被 `run_backtest.py` 装载 |
| `config/backtest.yaml` | 修改 | `data.source` 改为 `maxcompute`，新增 `data.cache` 与 `data.maxcompute` 配置块 |
| `config/secrets.yaml` | **新增** | 存放 AccessKey，**已加入 .gitignore，禁止入库** |
| `config/secrets.yaml.example` | **新增** | secrets.yaml 模板（入库） |
| `.gitignore` | 修改 | 新增 `config/secrets.yaml` 排除规则 |
| `run_backtest.py` | 修改 | 移除 akshare/tushare 装载分支，新增 `load_secrets()` 与 `build_maxcompute_data_source()` |
| `requirements.txt` | 修改 | 新增 `pyodps>=0.11.0` |
| `ARCHITECTURE.md` | 修改 | 数据层架构图与表格更新；新增 §4.6 切换理由、§4.7 表结构待补清单 |
| `engine/backtest.py` | 无 | 引擎层通过 `BaseDataSource` 抽象接口访问数据，无感知 |
| `strategy/*.py` | 无 | 策略层不感知数据源实现 |
| `analytics/*.py` | 无 | 分析层不感知数据源实现 |

**对回测可复现性的影响**：长期看可复现性提升（数据源稳定）。短期内由于 MaxCompute 表结构待补充，方法会抛 `NotImplementedError`，回测**暂时无法实际运行**，须等表结构填充完成后方能跑通。

---

## 4. 关键文件路径与现有函数签名

### 4.1 `data_layer/base_data_source.py` — 抽象基类（不变）

```python
class BaseDataSource(ABC):
    @abstractmethod
    def get_bars(self, code: str, start_date: str, end_date: str,
                 period: str = "daily", adjust: str = "qfq") -> pd.DataFrame: ...

    @abstractmethod
    def get_stock_list(self) -> pd.DataFrame: ...

    @abstractmethod
    def get_index_constituents(self, index_code: str) -> List[str]: ...
```

### 4.2 `run_backtest.py` — 数据源装载（修改前）

```python
data_cfg = cfg.get("data", {})
source_type = data_cfg.get("source", "akshare")
if source_type == "tushare":
    data_source = TushareDataSource(token=data_cfg.get("tushare_token", ""))
else:
    data_source = AKShareDataSource()
```

### 4.3 `data_layer/maxcompute_source.py` — 新增类签名

```python
class MaxComputeDataSource(BaseDataSource):
    def __init__(
        self,
        access_id: str,
        access_key: str,
        project: str,
        endpoint: str,
        storage: Optional[LocalStorage] = None,
        use_cache: bool = True,
        cache_retention_days: int = 7,
        cache_max_size_gb: float = 1.0,
        tables: Optional[Dict[str, str]] = None,
    ) -> None: ...

    def get_bars(self, code, start_date, end_date,
                 period="daily", adjust="qfq") -> pd.DataFrame: ...
    def get_stock_list(self) -> pd.DataFrame: ...
    def get_index_constituents(self, index_code: str) -> List[str]: ...

    # 内部
    def _get_odps(self): ...                       # 懒加载 ODPS 连接
    def _cleanup_cache(self) -> None: ...          # 按 retention + size 清理本地缓存
    def _require_table(self, key: str) -> str: ... # 校验表名配置
    def _fetch_daily_bars(...): ...                # SQL 待补充
    def _fetch_minute_bars(...): ...               # SQL 待补充
    def _execute_sql(self, sql: str) -> pd.DataFrame: ...
```

---

## 5. 需求详情

### 5.1 功能目标

1. **新增 MaxCompute 数据源**：实现 `MaxComputeDataSource`，继承 `BaseDataSource`，覆盖全部三个抽象方法。
2. **完全切换**：`run_backtest.py` 不再装载 akshare/tushare，固定使用 MaxCompute。akshare/tushare 源码保留以便将来按需切换。
3. **凭据隔离**：AccessKey 通过 `config/secrets.yaml`（gitignored）或环境变量 `MAXCOMPUTE_ACCESS_ID` / `MAXCOMPUTE_ACCESS_KEY` 注入，禁止硬编码或入库。
4. **本地缓存复用**：MaxCompute 拉到的数据写入现有 `LocalStorage`，命中则跳过 SQL 查询，节省计费成本。
5. **缓存清理策略**：实例化数据源时自动清理本地缓存：
   - 删除修改时间超过 `data.cache.retention_days` 的文件（默认 7 天）。
   - 若总大小仍超过 `data.cache.max_size_gb`（默认 1.0 GB），按 mtime 升序继续删除最旧文件。
6. **错误提示清晰**：当表结构未补充时（`data.maxcompute.tables.*` 为空），调用 `get_bars/get_stock_list/get_index_constituents` 抛出 `NotImplementedError`，错误信息中明确指出待补充的字段清单，**不静默失败**。
7. **表结构待补充信息留痕**：在 `maxcompute_source.py` 顶部 docstring、`ARCHITECTURE.md §4.7`、`config/backtest.yaml` 注释三处同步记录待补清单，便于后续填充。

### 5.2 凭据加载优先级

```text
环境变量 (MAXCOMPUTE_ACCESS_ID / MAXCOMPUTE_ACCESS_KEY)
    ↓ 若未设置
config/secrets.yaml -> maxcompute.access_id / access_key
    ↓ 若两者皆缺失
抛出 RuntimeError，提示配置方法
```

### 5.3 交互流程

```text
用户运行 python run_backtest.py --strategy ...
│
├─ load_config() 读取 config/backtest.yaml
├─ load_secrets() 读取 config/secrets.yaml（缺失则返回空 dict）
├─ build_maxcompute_data_source(cfg)
│   ├─ 合并环境变量 + secrets，校验非空
│   ├─ 实例化 MaxComputeDataSource
│   │   └─ 构造函数中 _cleanup_cache()：按 retention + max_size 清理
│   └─ 返回数据源对象
└─ BacktestEngine 调用 data_source.get_bars(...)
    ├─ 1. 命中本地缓存且完全覆盖 → 直接返回缓存
    ├─ 2. 未命中 → _fetch_daily_bars() 执行 MaxCompute SQL
    │      (当前抛 NotImplementedError，待表结构补充)
    ├─ 3. 标准化列名为 [code, date, open, high, low, close, volume, amount]
    ├─ 4. 与本地缓存合并去重、写回 Parquet
    └─ 5. 返回请求区间内的数据
```

---

## 6. 配置变更

### 6.1 `config/backtest.yaml`

修改 `data` 块：

```yaml
data:
  source: "maxcompute"
  cache_dir: "data/raw"
  storage_format: "parquet"

  cache:
    retention_days: 7
    max_size_gb: 1.0

  maxcompute:
    endpoint: "http://service.cn-beijing.maxcompute.aliyun.com/api"
    project: "a_share_historical_data"
    tables:
      daily: ""
      minute: ""
      stock_info: ""
      index_constituent: ""
```

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `data.source` | str | `maxcompute` | 数据源标识，当前固定使用 MaxCompute |
| `data.cache.retention_days` | int | `7` | 本地缓存保留天数，超过自动清理 |
| `data.cache.max_size_gb` | float | `1.0` | 缓存目录最大体积（GB），超过则按 mtime 升序清理 |
| `data.maxcompute.endpoint` | str | 北京区 URL | MaxCompute Endpoint，按项目所在区域填写 |
| `data.maxcompute.project` | str | `a_share_historical_data` | MaxCompute 项目名 |
| `data.maxcompute.tables.daily` | str | `""` | **待补充**：日K表名 |
| `data.maxcompute.tables.minute` | str | `""` | **待补充**：分钟K表名（可选） |
| `data.maxcompute.tables.stock_info` | str | `""` | **待补充**：股票基础信息表名 |
| `data.maxcompute.tables.index_constituent` | str | `""` | **待补充**：指数成分股表名 |

### 6.2 `config/secrets.yaml`（新增，不入库）

```yaml
maxcompute:
  access_id: "<your_access_id>"
  access_key: "<your_access_key>"
```

### 6.3 `config/secrets.yaml.example`（新增，入库）

提供占位符模板，方便他人/CI 复制后填充。

### 6.4 `.gitignore`

新增一行 `config/secrets.yaml`。

### 6.5 `requirements.txt`

新增 `pyodps>=0.11.0`。

---

## 7. 不可改动的红线区域

1. **`BaseDataSource` 抽象接口不可变**：`get_bars` / `get_stock_list` / `get_index_constituents` 的签名、返回类型、列结构必须保持不变。所有数据源对上层透明。
2. **`LocalStorage` 文件命名与目录结构不可变**：`data/raw/daily/{code}_{period}.parquet`。MaxCompute 数据源直接复用，不重新设计缓存格式。
3. **凭据禁止入库**：`config/secrets.yaml` 必须在 `.gitignore` 中。任何 commit、PR、日志、错误信息都不允许暴露 AccessKey 明文。
4. **缓存清理只删本框架自己写入的缓存**：清理逻辑作用域限定在 `LocalStorage` 的根目录，不得递归删除项目根下其他目录。
5. **AKShare/Tushare 源码保留**：`data_layer/akshare_source.py` 与 `data_layer/tushare_source.py` 文件本身不删除，以便将来按需切换；仅从 `run_backtest.py` 的装载路径移除。
6. **回测可复现性**：缓存清理策略不得影响"相同数据 + 相同策略 + 相同参数 → 相同结果"。即缓存清理只影响"是否需要二次拉取 MaxCompute"，不影响最终返回的数据内容。
7. **engine / strategy / account / analytics 层一律不动**：所有改动局限在 data_layer + config + run_backtest + 文档。

---

## 8. 修改范围与位置

### 8.1 主要修改文件

| 文件 | 修改位置 | 修改内容 |
|------|----------|----------|
| `data_layer/maxcompute_source.py` | 新建文件 | 实现 `MaxComputeDataSource` 类（连接、缓存清理、SQL 骨架与待补 TODO） |
| `data_layer/__init__.py` | `from ... import` 与 `__all__` | 导出 `MaxComputeDataSource` |
| `config/secrets.yaml` | 新建文件 | 写入真实凭据 |
| `config/secrets.yaml.example` | 新建文件 | 模板占位符 |
| `.gitignore` | 末尾追加 | 新增 `config/secrets.yaml` 排除规则 |
| `config/backtest.yaml` | `data` 块 | 改 `source`，新增 `cache` 与 `maxcompute` 子块 |
| `run_backtest.py` | imports 与 `main()` 前半段 | 移除 akshare/tushare 装载，新增 `load_secrets()` 与 `build_maxcompute_data_source()` |
| `requirements.txt` | 末尾追加 | 新增 `pyodps>=0.11.0` |
| `ARCHITECTURE.md` | §1 架构图、§2.1 表格、§6 速查表、§4 新增 §4.6/§4.7、§5.1 扩展指南 | 同步反映数据源切换 |

### 8.2 不修改的文件

- `engine/backtest.py` / `engine/trade_engine.py` / `engine/paper_trader.py`
- `strategy/*.py`
- `account/portfolio.py` / `account/position.py`
- `analytics/*.py`
- `utils/calendar.py` / `utils/logger.py`
- `data_layer/base_data_source.py` / `data_layer/local_storage.py`
- `data_layer/akshare_source.py` / `data_layer/tushare_source.py`（保留原状）

---

## 9. 验收标准

### 9.1 功能验收（当前阶段：表结构未补充）

- [x] `config/secrets.yaml` 被 `.gitignore` 排除（`git check-ignore` 与 `git status` 双重验证）。
- [x] AccessKey 字符串只出现在 `config/secrets.yaml` 一处，不在任何入库文件中可搜到。
- [x] 所有改动文件 `python -m py_compile` / `ast.parse` 通过。
- [x] `config/backtest.yaml` 中 `data.source` = `maxcompute`，endpoint = 北京区，project = `a_share_historical_data`。
- [x] 缺凭据时 `python run_backtest.py --strategy ...` 立即报错，提示如何配置；不抛出栈追踪、不沉默 hang。
- [x] 表结构未配置时调用 `get_bars()` 抛出 `NotImplementedError`，错误信息列出待补字段清单，**不返回空 DataFrame 沉默失败**。
- [x] 缓存清理逻辑在实例化时自动执行，日志中可见"缓存清理：删除 N 个超期文件"或"删除 N 个最旧文件以满足 X GB 上限"。

### 9.2 功能验收（表结构补充后）

- [ ] `MaxComputeDataSource.get_bars("510300.SH", "20220101", "20231231")` 返回非空 DataFrame，列为 `[code, date, open, high, low, close, volume, amount]`，`date` 为 `YYYYMMDD` 字符串。
- [ ] 二次请求相同区间命中本地缓存，**不触发 MaxCompute SQL**（可通过日志或 SQL 计费记录验证）。
- [ ] `get_stock_list()` 返回包含 `[code, name, list_date, industry]` 列的 DataFrame。
- [ ] `get_index_constituents("000300.SH")` 返回非空成分股代码列表。

### 9.3 回测验证（表结构补充后）

使用 `DoubleMAStrategy` + `510300.SH` 在 `20220101 ~ 20231231` 区间回测：

```
用例：双均线策略 + 沪深300ETF
- 输入：strategy=DoubleMAStrategy, code=510300.SH, [20220101, 20231231], capital=1_000_000
- 修改前行为：AKShare 模式可跑通；Tushare 模式因积分不足失败
- 修改后预期行为：MaxCompute 模式可跑通，输出累计收益率、最大回撤、夏普比率；
  二次运行命中缓存后耗时显著下降（首次分钟级 → 二次秒级）
```

### 9.4 缓存清理验收

```
用例 1：超期清理
- 输入：data/raw 下手动 touch 一个 8 天前的 mtime 文件，retention_days=7
- 修改后预期行为：实例化 MaxComputeDataSource 时该文件被删除，日志显示"缓存清理：删除 1 个超期文件"

用例 2：容量清理
- 输入：data/raw 下累积文件总大小 1.5 GB，max_size_gb=1.0
- 修改后预期行为：实例化时按 mtime 升序删除最旧文件，直到总大小 ≤ 1.0 GB
```

---

## 10. 备注

1. **PRD 回溯说明**：本 PRD 为追溯补写。代码改动已先于 PRD 完成（在新增"做需求之前必须先写 PRD"规则之前），属于历史遗留。后续需求严格遵循"先写 PRD 再动手"。
2. **AccessKey 安全提醒**：当前 `config/secrets.yaml` 中的 AccessKey 已通过对话历史暴露过一次（贴在聊天里）。强烈建议在阿里云 RAM 控制台**禁用并轮换**这对 key，再更新 `config/secrets.yaml`。轮换不影响代码逻辑。
3. **MaxCompute SQL 计费**：MaxCompute 按扫描数据量计费。本框架按"单只股票 + 日期区间"粒度查询，单次扫描量可控；本地缓存命中后不再产生扫描。**强烈建议**待表结构确认后启用分区裁剪（如 `WHERE ds BETWEEN ...`），进一步降低扫描量。
4. **表结构待补充清单**：详见 `ARCHITECTURE.md §4.7` 或 `data_layer/maxcompute_source.py` 顶部 docstring。补充后无需改动框架其他部分，只需在 `_fetch_daily_bars` / `_fetch_minute_bars` / `get_stock_list` / `get_index_constituents` 内填充 SQL 并做列名映射即可。
5. **Lookahead Bias 风险**：MaxCompute 提供的是历史归档数据，不存在"使用未来数据"的风险。但若后续接入实时增量库表，需确认时间戳口径是否会包含 T 日盘后修订数据，避免引入回测偏差。
6. **未来扩展方向**：表结构补充完成后，可考虑实现 `get_multi_bars()` 的批量 SQL 优化版本（一次 SQL 拉多只股票，IN 子句 + 单次扫描），显著提升多标的策略的回测速度。当前继承自基类的串行实现已可工作，无需立即优化。
