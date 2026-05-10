# A-Share Quantitative Trading

A股模拟量化交易系统 —— 纯模拟、无真实交易，支持策略研发、历史回测、虚拟盘跟踪与绩效评估。

> ⚠️ **免责声明**：本系统仅用于模拟与学术研究，不产生任何真实委托，不涉及资金划转。

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 运行示例策略回测

```bash
python run_backtest.py --strategy strategy.double_ma.DoubleMAStrategy --start 20210101 --end 20231231
```

### 3. 查看报告

回测结束后，图表与 HTML 报告默认输出至 `output/report/` 目录。

---

## 项目结构

```
.
├── config/                    # 配置文件
│   └── backtest.yaml          # 回测参数、费率、滑点
├── data/                      # 数据目录
│   ├── raw/                   # 原始下载数据
│   └── processed/             # 清洗后数据
├── data_layer/                # 数据层
│   ├── base_data_source.py    # 数据源抽象基类
│   ├── akshare_source.py      # AKShare 实现
│   └── local_storage.py       # 本地数据缓存
├── engine/                    # 引擎层
│   ├── backtest.py            # 回测主引擎
│   ├── trade_engine.py        # 撮合与费用计算
│   └── paper_trader.py        # 虚拟盘
├── strategy/                  # 策略层
│   ├── base_strategy.py       # 策略基类
│   ├── double_ma.py           # 双均线示例
│   ├── momentum.py            # 动量示例
│   └── multi_factor.py        # 多因子示例
├── account/                   # 账户层
│   ├── portfolio.py           # 账户与持仓
│   └── position.py            # 单只股票持仓（T+1）
├── analytics/                 # 绩效分析
│   ├── metrics.py             # 收益/风险指标
│   ├── plotter.py             # 可视化
│   └── report.py              # HTML 报告
├── utils/                     # 工具模块
│   ├── calendar.py            # A股交易日历
│   └── logger.py              # 日志配置
├── run_backtest.py            # CLI 入口
├── requirements.txt
└── README.md
```

---

## 编写自定义策略

继承 `BaseStrategy` 并实现 `handle_data` 方法：

```python
from strategy.base_strategy import BaseStrategy, Context

class MyStrategy(BaseStrategy):
    def initialize(self, context: Context) -> None:
        super().initialize(context)
        self.set_universe(["000001.SZ", "600000.SH"])

    def handle_data(self, context: Context, data) -> None:
        # 交易逻辑
        context.order("000001.SZ", 100)
```

运行：
```bash
python run_backtest.py --strategy strategy.my_strategy.MyStrategy
```

---

## 核心特性

- **A股规则模拟**：T+1、涨跌停、最小100股、印花税/佣金/过户费
- **滑点模型**：百分比或固定金额滑点
- **数据缓存**：AKShare 数据自动落盘本地，支持增量更新
- **可扩展架构**：数据源、策略、交易规则均可插拔替换

---

## 技术栈

- Python 3.10+
- pandas / numpy
- akshare
- matplotlib
- pyyaml

---

*本项目仅供学习研究使用*
