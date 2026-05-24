"""可交易股票过滤 helper。

配套 PRD_20260524_13。

设计原则：
    - schema 与主分支 ``data_layer/bigquery_source.py`` 的 ``trading_permissions``
      字段名 / 默认值 / 含义保持完全一致，便于将来合并时无缝衔接
    - 本模块仅做"基于代码前缀"的快速过滤；ST / 退市等需要 ``dim_security``
      状态字段的过滤由 ``BigQueryDataSource._security_permission_filter_sql()``
      在 SQL 层完成

使用示例::

    from strategy.ml_multi_horizon_picker.tradable import (
        DEFAULT_TRADING_PERMISSIONS, filter_codes, is_tradable_code,
    )

    perms = {**DEFAULT_TRADING_PERMISSIONS, "allow_star_market": True}
    tradable = filter_codes(["600000.SH", "688001.SH", "300001.SZ"], perms)
    # → ["600000.SH", "688001.SH"]（创业板 300 仍被排除）
"""

from __future__ import annotations

from typing import Dict, List, Optional


# 默认权限：全部 False（保守）
# 与 data_layer/bigquery_source.py:_DEFAULT_TRADING_PERMISSIONS schema 完全一致
DEFAULT_TRADING_PERMISSIONS: Dict[str, bool] = {
    "allow_bse": False,                 # 北交所
    "allow_star_market": False,         # 科创板
    "allow_chinext": False,             # 创业板
    "allow_hk_stock_connect": False,    # 港股通
    "allow_neeq": False,                # 新三板
    "allow_risk_warning": False,        # ST / *ST
    "allow_delisting": False,           # 退市
    "allow_margin_trading": False,      # 融资融券
    "allow_stock_options": False,       # 股票期权
    "allow_convertible_bonds": False,   # 可转债
    "allow_cdr": False,                 # CDR
    "allow_unknown_security": False,    # 未知类型
}


def merge_permissions(
    overrides: Optional[Dict[str, bool]] = None,
) -> Dict[str, bool]:
    """把用户提供的 overrides 合并到默认权限上。"""
    merged = dict(DEFAULT_TRADING_PERMISSIONS)
    if overrides:
        merged.update({k: bool(v) for k, v in overrides.items()})
    return merged


def _bare_code(code: str) -> str:
    """剥离 ``.SH/.SZ/.BJ`` 后缀，返回 6 位数字代码。"""
    return str(code).split(".")[0].strip()


def _suffix(code: str) -> str:
    """返回大写后缀（SH/SZ/BJ/CSI/...），无后缀返回空串。"""
    s = str(code)
    if "." not in s:
        return ""
    return s.split(".")[-1].upper()


def is_tradable_code(
    code: str,
    permissions: Optional[Dict[str, bool]] = None,
) -> bool:
    """判断某代码是否在 ``permissions`` 下可交易（仅前缀级判断）。

    Args:
        code: 框架代码（``XXXXXX.SH`` / ``XXXXXX.SZ`` / ``XXXXXX.BJ``）
        permissions: 权限字典；缺省字段补 DEFAULT。None 视为全 False

    Returns:
        True = 可交易；False = 被某项权限禁止

    Note:
        本函数只能识别"看代码前缀就知道在哪个板块"的禁令。
        ST / 退市 / 融资标的等需要参考 dim_security 状态字段，
        本函数无法判断，需要数据源 SQL 层配合。
    """
    perms = merge_permissions(permissions)
    bare = _bare_code(code)
    suffix = _suffix(code)

    # 北交所
    if not perms["allow_bse"]:
        if suffix == "BJ":
            return False
        if bare.startswith(("43", "83", "87", "88", "920")):
            return False

    # 科创板
    if not perms["allow_star_market"]:
        if bare.startswith(("688", "689")):
            return False

    # 创业板
    if not perms["allow_chinext"]:
        if bare.startswith(("300", "301")):
            return False

    # 港股通：以 .HK 标记或 9 开头（个别）；当前数据源主要是 A 股，留接口
    if not perms["allow_hk_stock_connect"]:
        if suffix == "HK":
            return False

    # 新三板：与 BJ 类似但用 .NEEQ 后缀（如有）或部分 4/8 开头
    # 我们已在 allow_bse 处理 43/83/87/88，这里只处理 .NEEQ 后缀
    if not perms["allow_neeq"]:
        if suffix == "NEEQ":
            return False

    # 可转债（.SH 11x / .SZ 12x）
    if not perms["allow_convertible_bonds"]:
        if suffix == "SH" and bare.startswith("11"):
            return False
        if suffix == "SZ" and bare.startswith("12"):
            return False

    return True


def filter_codes(
    codes: List[str],
    permissions: Optional[Dict[str, bool]] = None,
) -> List[str]:
    """批量过滤，返回可交易代码列表（保持原顺序）。"""
    return [c for c in codes if is_tradable_code(c, permissions)]


def classify_board(code: str) -> str:
    """返回板块标签：``main_sh`` / ``main_sz`` / ``star`` / ``chinext`` /
    ``bse`` / ``convertible_bond_sh`` / ``convertible_bond_sz`` /
    ``etf_sh`` / ``etf_sz`` / ``unknown``。
    """
    bare = _bare_code(code)
    suffix = _suffix(code)

    if bare.startswith(("688", "689")):
        return "star"
    if bare.startswith(("300", "301")):
        return "chinext"
    if suffix == "BJ" or bare.startswith(("43", "83", "87", "88", "920")):
        return "bse"

    if suffix == "SH":
        if bare.startswith("11"):
            return "convertible_bond_sh"
        if bare.startswith(("51", "56", "58")):
            return "etf_sh"
        if bare.startswith("60"):
            return "main_sh"
    if suffix == "SZ":
        if bare.startswith("12"):
            return "convertible_bond_sz"
        if bare.startswith(("15", "16")):
            return "etf_sz"
        if bare.startswith(("00", "001", "002", "003")):
            return "main_sz"
    return "unknown"
