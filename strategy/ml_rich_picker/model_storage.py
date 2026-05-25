"""ml_rich_picker 专用模型存储 / 加载。

与父类 ``ml_multi_horizon_picker.model_storage`` 的区别：

- sell 模型从 **二分类 ``sell_v1``** 改为 **回归 ``sell_remaining_days_v1``**
  （预测未来 N 天内风险可控的最优卖出剩余天数，浮点）
- buy 模型沿用父类的 ``buy_h{1,5,10,20}.pkl`` 命名

两套 sell 模型物理文件名不同，可在同一目录共存而不会被互相覆盖，
也避免「老 binary 模型」被「新 regression 推理代码」误加载的 silent bug。

文件布局::

    {model_dir}/buy_h1.pkl
    {model_dir}/buy_h5.pkl
    {model_dir}/buy_h10.pkl
    {model_dir}/buy_h20.pkl
    {model_dir}/sell_remaining_days_v1.pkl
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from strategy.ml_multi_horizon_picker.model_storage import (
    BUY_HORIZONS,
    buy_model_path,
)
from strategy.ml_stock_picker.model_storage import load_model, save_model
from utils.logger import get_logger

logger = get_logger(__name__)


SELL_REMAINING_DAYS_MODEL_NAME: str = "sell_remaining_days_v1"


def sell_remaining_days_path(model_dir: str) -> str:
    """返回 ``sell_remaining_days_v1.pkl`` 的完整路径。"""
    base = model_dir.rstrip("/")
    return f"{base}/{SELL_REMAINING_DAYS_MODEL_NAME}.pkl"


def save_rich_bundle(
    model_dir: str,
    buy_models: Dict[int, Any],
    sell_model: Optional[Any] = None,
) -> None:
    """保存 rich 模型包（buy 4 个 + sell 回归 1 个）。

    Args:
        model_dir: 目标目录（本地或 ``gs://``）
        buy_models: ``{horizon: model}`` 字典
        sell_model: 卖出回归模型（可选）
    """
    for horizon, model in buy_models.items():
        save_model(model, buy_model_path(model_dir, horizon))
    if sell_model is not None:
        save_model(sell_model, sell_remaining_days_path(model_dir))
    logger.info(
        f"已保存 {len(buy_models)} 个 buy 模型"
        + (" + 1 个 sell_remaining_days 回归模型" if sell_model is not None else "")
    )


def load_rich_bundle(
    model_dir: str,
    horizons: Optional[List[int]] = None,
) -> Dict[str, Optional[Any]]:
    """加载 rich 模型包。

    Returns:
        ``{"buy_h1": ..., ..., "sell_remaining_days_v1": ...}``。
        缺失的模型对应 ``None``，由调用方决定降级行为。
    """
    hs = horizons or BUY_HORIZONS
    result: Dict[str, Optional[Any]] = {}
    for h in hs:
        result[f"buy_h{h}"] = load_model(buy_model_path(model_dir, h))
    result[SELL_REMAINING_DAYS_MODEL_NAME] = load_model(
        sell_remaining_days_path(model_dir)
    )
    loaded = [k for k, v in result.items() if v is not None]
    missing = [k for k, v in result.items() if v is None]
    if missing:
        logger.warning(
            f"已加载 {len(loaded)} 个模型 ({loaded}); 缺失 {len(missing)} 个 ({missing})"
        )
    else:
        logger.info(f"已加载全部 {len(loaded)} 个模型")
    return result
