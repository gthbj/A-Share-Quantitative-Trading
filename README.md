# A-Share Quantitative Trading

A 股模拟量化交易系统 —— 纯模拟、无真实交易，支持策略研发、历史回测、虚拟盘跟踪与绩效评估。

> ⚠️ **免责声明**：本系统仅用于模拟与学术研究，不产生任何真实委托，不涉及资金划转。

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
# 如需运行单元测试：
pip install -r requirements-dev.txt
```

### 2. 配置 BigQuery 凭据（首次运行必做）

数据源默认使用 Google Cloud BigQuery（项目 `data-aquarium`，dataset `ashare`）。
将凭据放入 `config/secrets.yaml`（已在 .gitignore 中，不会入库）：

```yaml
bigquery:
  credentials_path: "config/bigquery-service-account.json"
```

或通过环境变量（推荐）：

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/bigquery-service-account.json
```

> 如需回退到 MaxCompute，修改 `config/backtest.yaml` 中 `data.source: "maxcompute"` 并配置对应凭据。

### 3. 运行回测（推荐用 preset）

```bash
# 一键运行双均线策略（默认 510300.SH × 15min × 2016-2025）
python run_backtest.py --preset double_ma

# 覆盖时间区间
python run_backtest.py --preset double_ma --start 20240101 --end 20240601

# 换标的（多个用逗号）
python run_backtest.py --preset double_ma --universe "510300.SH,510500.SH"
```

### 4. 兼容旧用法（直接指定策略类）

```bash
python run_backtest.py \
  --strategy strategy.double_ma.DoubleMAStrategy \
  --start 20240101 --end 20240601 --universe 510300.SH
```

### 5. 查看报告

回测产物落到：
- `--preset` 模式：`strategy/<preset>/runs/<timestamp>/`
- 旧用法：`output/<timestamp>/`

每个目录包含：
| 文件 | 内容 |
|------|------|
| `report.html` | 完整 HTML 报告（含策略元信息、绩效、交易明细、费用） |
| `summary.md` | 同源 Markdown 说明（含一句话评价） |
| `trades.csv` | 完整成交流水 |
| `cum_returns.png` / `drawdown.png` / `monthly_returns.png` | 收益曲线、回撤、月度热力图 |

---

## 项目结构

```
.
├── PRD/                          # 需求文档（按日期编号）
├── PROJECT_OWNER_PREFERENCES.md  # 项目所有者偏好
├── ARCHITECTURE.md               # 架构设计文档
├── config/
│   ├── backtest.yaml             # 全局配置：费率、滑点、撮合规则、表名
│   ├── secrets.yaml              # BigQuery / MaxCompute 凭据（不入库）
│   └── secrets.yaml.example      # 凭据模板
├── data/                         # 本地缓存
│   └── raw/                      # BigQuery 拉取的行情 Parquet（不入库）
├── data_layer/
│   ├── base_data_source.py       # 数据源抽象基类
│   ├── bigquery_source.py        # BigQuery 实现（默认）
│   ├── maxcompute_source.py      # MaxCompute 实现（备选）
│   ├── akshare_source.py         # AKShare 实现（备选，未启用）
│   ├── tushare_source.py         # Tushare 实现（备选，未启用）
│   └── local_storage.py          # Parquet 缓存
├── engine/
│   ├── backtest.py               # 回测主引擎（日线 + 分钟级双频）
│   ├── trade_engine.py           # 撮合（含 MARKET/LIMIT/STOP）+ A 股规则 + 全局止损
│   └── paper_trader.py           # 虚拟盘（状态持久化）
├── strategy/                     # 策略实验包
│   ├── base_strategy.py          # 基类与 Context
│   ├── double_ma/                # ★ 完整实验包形态（推荐）
│   │   ├── strategy.py
│   │   ├── config.yaml           # preset 默认参数
│   │   ├── README.md
│   │   └── runs/                 # 历次回测产物
│   ├── momentum.py               # 月度动量（单文件示例）
│   ├── multi_factor.py           # 多因子（单文件示例）
│   └── intraday_ma.py            # 日内双均线（单文件示例）
├── account/
│   ├── portfolio.py              # 虚拟账户
│   └── position.py               # 单只股票持仓（T+1）
├── analytics/
│   ├── metrics.py                # 收益/风险/FIFO 配对盈亏指标
│   ├── plotter.py                # 可视化（自动探测中文字体）
│   ├── report.py                 # HTML 报告
│   └── summary.py                # Markdown 报告
├── utils/
│   ├── calendar.py               # A 股交易日历（chinese_calendar 接入）
│   ├── code.py                   # 股票代码归一化
│   └── logger.py                 # 日志配置
├── tests/                        # 单元测试
├── run_backtest.py               # CLI 入口
├── requirements.txt
├── requirements-dev.txt
└── pytest.ini
```

---

## 策略实验包（推荐组织方式）

每个长期保留的策略建议放入 `strategy/<name>/` 子目录：

```
strategy/<name>/
├── strategy.py           # 策略类实现
├── __init__.py           # 重新导出策略类（保持原有 import 路径）
├── config.yaml           # preset 默认参数（universe / 时间区间 / 频率 / 基准）
├── README.md             # 策略说明（信号、参数、适用市场、局限）
└── runs/                 # 每次回测的产物（默认 gitignore）
```

`config.yaml` 示例（节选自 `strategy/double_ma/config.yaml`）：

```yaml
name: "double_ma"
class: "strategy.double_ma.DoubleMAStrategy"
params:
  short_window: 5
  long_window: 20
  universe: ["510300.SH"]
backtest:
  start_date: "20160101"
  end_date: "20251231"
  frequency: "15min"
  initial_capital: 1000000
  benchmark: "000300.SH"
```

参数优先级（高到低）：**CLI 参数 > preset config > 全局 backtest.yaml > 内置默认**。

---

## 编写自定义策略

继承 `BaseStrategy` 并实现 `handle_data`：

```python
from strategy.base_strategy import BaseStrategy, Context

class MyStrategy(BaseStrategy):
    DEFAULT_UNIVERSE = ["510300.SH"]  # 可被 CLI 或 preset 覆盖

    def __init__(self, universe=None):
        super().__init__()
        self._init_universe = list(universe) if universe else list(self.DEFAULT_UNIVERSE)

    def initialize(self, context: Context) -> None:
        super().initialize(context)
        self.set_universe(self._init_universe)

    def handle_data(self, context: Context, data) -> None:
        # 市价单
        context.order("510300.SH", 100)
        # 限价单（PRD_20260520_06 新增）
        context.limit_order("510300.SH", 100, price=10.0)
        # 止损单
        context.stop_order("510300.SH", -100, stop_price=9.5)
```

运行：

```bash
python run_backtest.py --strategy strategy.my_module.MyStrategy --universe 510300.SH
```

---

## 核心特性

- **A 股规则模拟**：T+1、涨跌停（±10%）、最小 100 股、印花税/佣金/过户费
- **三种订单类型**：MARKET / LIMIT / STOP
- **撮合规则**：默认 next_open（信号 T → 成交 T+1 开盘），避免 Lookahead Bias
- **全局止损**：可在 `backtest.yaml` 配置，与策略订单并轨执行
- **BigQuery 数据源**：默认接入 Google Cloud `data-aquarium` 项目 `ashare` dataset（含 ODS/DWD/DWS/ADS 分层），本地 Parquet 缓存
- **复权**：日K线表内置 `adjust_type`（none/qfq/hfq），直接查询对应复权数据
- **绩效指标**：累计/年化收益、最大回撤、夏普/索提诺、Beta/Alpha/IR、胜率、盈亏比（FIFO 配对）
- **可视化**：累计收益、回撤、月度热力图；自动探测系统中文字体
- **单元测试**：tests/ 覆盖 TradeEngine / Position / Portfolio / metrics / code

---

## 单元测试

```bash
pytest tests/ -v
# 期望：所有用例 PASS
```

测试聚焦于易出 bug 的边界（T+1 解冻、FIFO 配对、涨跌停撮合、限价/止损触发等）。

---

## 技术栈

- Python 3.9+
- pandas / numpy
- google-cloud-bigquery（BigQuery 数据源）
- pyodps（MaxCompute 数据源，备选）
- matplotlib
- pyyaml
- chinese-calendar
- pytest（开发）

---

## 文档索引

| 文档 | 用途 |
|------|------|
| `PROJECT_OWNER_PREFERENCES.md` | 项目所有者偏好（commit 规范、PRD 规范） |
| `ARCHITECTURE.md` | 架构设计与模块依赖（**改代码前必读**） |
| `TODO.md` | 已知问题与待优化清单 |
| `PRD/PRD_*.md` | 各次需求的设计文档 |
| `strategy/<name>/README.md` | 单策略说明（信号/参数/适用市场） |

---

*本项目仅供学习研究使用。*
