# BQML Signal Picker

读取 `ashare.ads_signal_ml_stock_picker_bqml_1d` 的 BigQuery ML 候选信号，并交给现有回测引擎真实撮合。

默认参数面向 10 万元账户：

- 初始资金：100,000
- 每日候选：`score_rank <= 10`
- 最大持仓：3 只
- 固定持有期：5 个交易日
- 总仓位上限：95%
- 不重复加仓已持有股票
- 是否叠加止损由全局 `config/backtest.yaml` 的 `stop_loss` 控制；当前默认会叠加 5% 全局止损

运行：

```bash
ASHARE_USE_GCLOUD_ACCESS_TOKEN=1 python run_backtest.py --preset bqml_signal_picker
```
