"""ml_multi_horizon_picker 走步回测相关单元测试（PRD_20260524_13）。

覆盖：
- tradable：过滤北交所/科创板/创业板/可转债、merge_permissions、classify_board
- model_registry：JSON 序列化、find_for_date 边界
- strategy 走步模式：_maybe_switch_model 行为
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from strategy.ml_multi_horizon_picker.model_registry import (
    ModelRegistry,
    RegistryEntry,
    build_registry,
)
from strategy.ml_multi_horizon_picker.strategy import MLMultiHorizonStrategy
from strategy.ml_multi_horizon_picker.tradable import (
    DEFAULT_TRADING_PERMISSIONS,
    classify_board,
    filter_codes,
    is_tradable_code,
    merge_permissions,
)
from account.portfolio import Portfolio
from strategy.base_strategy import Context


# ───────────────────────────── tradable.py ─────────────────────────────


def test_default_permissions_all_false():
    """默认权限保守：全部 False。"""
    assert all(v is False for v in DEFAULT_TRADING_PERMISSIONS.values())


def test_merge_permissions_overrides():
    merged = merge_permissions({"allow_star_market": True})
    assert merged["allow_star_market"] is True
    assert merged["allow_bse"] is False  # 未指定保持默认


def test_is_tradable_code_main_board_pass():
    """主板沪/深默认可交易。"""
    assert is_tradable_code("600000.SH") is True
    assert is_tradable_code("000001.SZ") is True


def test_is_tradable_code_blocks_star_chinext_bse_by_default():
    """默认权限下，科创/创业/北交所均被禁。"""
    assert is_tradable_code("688001.SH") is False  # 科创板
    assert is_tradable_code("689001.SH") is False  # 科创板
    assert is_tradable_code("300001.SZ") is False  # 创业板
    assert is_tradable_code("301001.SZ") is False  # 创业板
    assert is_tradable_code("430001.BJ") is False  # 北交所 .BJ
    assert is_tradable_code("830001") is False  # 北交所裸代码 83 开头
    assert is_tradable_code("920001") is False  # 北交所 920 开头


def test_is_tradable_code_opens_when_granted():
    perms = {"allow_star_market": True}
    assert is_tradable_code("688001.SH", perms) is True
    # 即使开了科创板，创业板/北交所仍禁
    assert is_tradable_code("300001.SZ", perms) is False
    assert is_tradable_code("430001.BJ", perms) is False


def test_filter_codes_returns_tradable_only():
    """PRD §8 用例 1：filter_codes 默认权限下只保留主板。"""
    codes = ["600000.SH", "688001.SH", "300001.SZ", "000001.SZ", "430001.BJ"]
    result = filter_codes(codes)
    assert result == ["600000.SH", "000001.SZ"]


def test_filter_codes_preserves_order():
    codes = ["000001.SZ", "600000.SH", "300001.SZ"]
    result = filter_codes(codes)
    assert result == ["000001.SZ", "600000.SH"]


def test_filter_codes_convertible_bond_blocked():
    perms = {"allow_convertible_bonds": False}
    assert is_tradable_code("113001.SH", perms) is False
    assert is_tradable_code("123001.SZ", perms) is False


def test_classify_board():
    assert classify_board("600000.SH") == "main_sh"
    assert classify_board("000001.SZ") == "main_sz"
    assert classify_board("688001.SH") == "star"
    assert classify_board("300001.SZ") == "chinext"
    assert classify_board("430001.BJ") == "bse"
    assert classify_board("510300.SH") == "etf_sh"
    assert classify_board("159949.SZ") == "etf_sz"
    assert classify_board("113001.SH") == "convertible_bond_sh"


# ───────────────────────────── model_registry.py ─────────────────────────────


def test_registry_find_returns_latest_before():
    """PRD §8 用例 2：返回 train_end < current 中最大那个。"""
    reg = build_registry(
        "models/walk_forward",
        ["20191231", "20200131", "20200229"],
    )
    assert reg.find_for_date("20200215") == "models/walk_forward/20200131"
    # 月末当天收盘后才训练完成，因此当天仍只能使用上一个模型。
    assert reg.find_for_date("20200229") == "models/walk_forward/20200131"
    assert reg.find_for_date("20200301") == "models/walk_forward/20200229"


def test_registry_find_returns_none_when_before_all():
    """PRD §8 用例 3：当前日早于所有时点返回 None。"""
    reg = build_registry("models/walk_forward", ["20191231"])
    assert reg.find_for_date("20190630") is None
    assert reg.find_for_date("20191230") is None
    # 边界：等于第一个训练时点当天不可用，下一天才可用。
    assert reg.find_for_date("20191231") is None
    assert reg.find_for_date("20200101") == "models/walk_forward/20191231"


def test_registry_handles_date_format_variants():
    reg = build_registry("models/walk_forward", ["20200229"])
    assert reg.find_for_date("2020-03-15") == "models/walk_forward/20200229"
    assert reg.find_for_date("20200315") == "models/walk_forward/20200229"


def test_registry_json_roundtrip(tmp_path: Path):
    reg = build_registry("models/walk_forward", ["20191231", "20200131"])
    p = tmp_path / "registry.json"
    reg.to_json(p)
    data = json.loads(p.read_text("utf-8"))
    assert data["model_root"] == "models/walk_forward"
    assert len(data["entries"]) == 2
    assert data["entries"][0]["train_end_date"] == "20191231"

    reloaded = ModelRegistry.from_json(p)
    assert len(reloaded) == 2
    assert reloaded.find_for_date("20200115") == "models/walk_forward/20191231"


def test_registry_sorts_unordered_input():
    reg = ModelRegistry(
        entries=[
            RegistryEntry("20200229", "x/2"),
            RegistryEntry("20191231", "x/0"),
            RegistryEntry("20200131", "x/1"),
        ],
    )
    entries = reg.list_entries()
    assert [e.train_end_date for e in entries] == ["20191231", "20200131", "20200229"]


# ───────────────────────────── strategy walk-forward integration ─────────────────────────────


def test_strategy_constructor_with_registry_path(tmp_path: Path):
    """传入 model_registry_path 时 strategy 应能构造（实际加载在 initialize 中）。"""
    reg = build_registry("models/walk_forward", ["20191231"])
    reg_path = tmp_path / "registry.json"
    reg.to_json(reg_path)

    strat = MLMultiHorizonStrategy(
        model_registry_path=str(reg_path),
        target_position_count=10,
    )
    assert strat.model_registry_path == str(reg_path)
    assert strat._model_registry is None  # initialize 前还没加载


def test_strategy_universe_filter_via_permissions():
    """传入 trading_permissions 时初始化阶段就过滤掉禁止板块。"""
    universe = [
        "600000.SH",     # 主板，留
        "688001.SH",     # 科创，去
        "300001.SZ",     # 创业，去
        "000001.SZ",     # 主板，留
        "430001.BJ",     # 北交所，去
    ]
    strat = MLMultiHorizonStrategy(
        universe=universe,
        trading_permissions={"allow_star_market": False, "allow_chinext": False, "allow_bse": False},
    )
    assert set(strat._init_universe) == {"600000.SH", "000001.SZ"}


def test_strategy_no_filter_when_permissions_none():
    """trading_permissions=None 时不过滤（向后兼容）。"""
    universe = ["600000.SH", "688001.SH", "430001.BJ"]
    strat = MLMultiHorizonStrategy(universe=universe, trading_permissions=None)
    assert set(strat._init_universe) == set(universe)


def test_strategy_traditional_mode_unchanged():
    """没有 model_registry_path 时走传统模式，行为同 PRD_12。"""
    strat = MLMultiHorizonStrategy(model_dir="some/dir")
    assert strat.model_registry_path is None
    assert strat._model_registry is None


class _LiquidityDataSource:
    def __init__(self):
        self.calls = []

    def get_liquidity_top_equities(self, as_of_date, top_n, lookback_days, adjust):
        self.calls.append((as_of_date, top_n, lookback_days, adjust))
        return ["600000.SH", "300001.SZ", "000001.SZ"]


def test_strategy_initializes_liquidity_universe():
    """未显式传 universe 时，可从数据源按流动性初始化股票池并继续应用权限过滤。"""
    data_source = _LiquidityDataSource()
    ctx = Context(
        portfolio=Portfolio(initial_capital=100_000),
        data_source=data_source,
        current_date="20200102",
        frequency="daily",
    )
    strat = MLMultiHorizonStrategy(
        universe_source="liquidity_top",
        liquidity_top_n=500,
        liquidity_lookback_days=60,
        trading_permissions={"allow_chinext": False},
    )

    strat.initialize(ctx)

    assert data_source.calls == [("20200102", 500, 60, "qfq")]
    assert strat.get_universe() == ["600000.SH", "000001.SZ"]
