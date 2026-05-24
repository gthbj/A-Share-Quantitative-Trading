"""多模型存储 / 加载封装。

本策略需要管理 5 个模型（buy_h1/h5/h10/h20 + sell_v1），
每个独立 pickle 文件，统一在一个目录下：

    {model_dir}/buy_h1.pkl
    {model_dir}/buy_h5.pkl
    {model_dir}/buy_h10.pkl
    {model_dir}/buy_h20.pkl
    {model_dir}/sell_v1.pkl

复用 strategy/ml_stock_picker/model_storage 的本地/GCS 抽象。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from strategy.ml_stock_picker.model_storage import load_model, save_model
from utils.logger import get_logger

logger = get_logger(__name__)


BUY_HORIZONS: List[int] = [1, 5, 10, 20]
SELL_MODEL_NAME: str = "sell_v1"


def buy_model_path(model_dir: str, horizon: int) -> str:
    """返回 buy_h{horizon}.pkl 路径。"""
    base = model_dir.rstrip("/")
    return f"{base}/buy_h{horizon}.pkl"


def sell_model_path(model_dir: str) -> str:
    """返回 sell_v1.pkl 路径。"""
    base = model_dir.rstrip("/")
    return f"{base}/{SELL_MODEL_NAME}.pkl"


def save_bundle(
    model_dir: str,
    buy_models: Dict[int, Any],
    sell_model: Optional[Any] = None,
) -> None:
    """保存一组模型。

    Args:
        model_dir: 目标目录（本地或 gs://）
        buy_models: {horizon: model} 字典
        sell_model: 卖出模型（可选）
    """
    for horizon, model in buy_models.items():
        save_model(model, buy_model_path(model_dir, horizon))
    if sell_model is not None:
        save_model(sell_model, sell_model_path(model_dir))
    logger.info(
        f"已保存 {len(buy_models)} 个 buy 模型"
        + (" + 1 个 sell 模型" if sell_model is not None else "")
    )


def load_bundle(
    model_dir: str,
    horizons: Optional[List[int]] = None,
) -> Dict[str, Optional[Any]]:
    """加载一组模型。

    Args:
        model_dir: 模型目录
        horizons: 期望加载的 buy horizons，默认 [1, 5, 10, 20]

    Returns:
        字典 {"buy_h1": model_or_None, ..., "sell_v1": model_or_None}
        缺失的模型对应值为 None，由调用方决定是否走 deterministic fallback。
    """
    hs = horizons or BUY_HORIZONS
    result: Dict[str, Optional[Any]] = {}
    for h in hs:
        result[f"buy_h{h}"] = load_model(buy_model_path(model_dir, h))
    result[SELL_MODEL_NAME] = load_model(sell_model_path(model_dir))
    loaded = [k for k, v in result.items() if v is not None]
    missing = [k for k, v in result.items() if v is None]
    if missing:
        logger.warning(
            f"已加载 {len(loaded)} 个模型 ({loaded}); 缺失 {len(missing)} 个 ({missing})"
        )
    else:
        logger.info(f"已加载全部 {len(loaded)} 个模型")
    return result
