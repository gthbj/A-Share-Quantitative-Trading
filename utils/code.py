"""股票代码归一化与映射工具。

统一框架内的代码格式规范，供 CLI / data_layer / engine / strategy 共享。

约定：
  - 框架代码：``XXXXXX.SH`` / ``XXXXXX.SZ``（大写后缀）
  - 表代码：``shXXXXXX`` / ``szXXXXXX``（小写前缀，仅部分 MaxCompute 表内使用）
  - 裸 6 位代码：按前缀推断交易所；无法识别则抛 ValueError
"""

from __future__ import annotations

import datetime as _dt
from typing import List

from utils.calendar import TradingCalendar

# 上交所代码前缀（沪市主板 / 科创板 / ETF / LOF / 转债）
_SH_PREFIXES = ("60", "68", "51", "56", "58", "11")
# 深交所代码前缀（深市主板 / 创业板 / ETF / LOF）
_SZ_PREFIXES = ("00", "30", "15", "16")
# 北交所代码前缀（含老三板 43、普通 83/87/88、精选层 92）
_BJ_PREFIXES = ("43", "83", "87", "88", "92")

# 创业板注册制改革：2020-08-24 起涨跌幅由 ±10% 调整为 ±20%
_CHINEXT_REFORM_DATE = _dt.date(2020, 8, 24)

# 新股上市首日规则（2023 全面注册制后统一）：
# 所有板块上市前 N 个交易日不设涨跌幅，第 N+1 个交易日起恢复常规板块规则。
_NEW_LISTING_NO_LIMIT_DAYS = 5


def normalize_code(raw: str) -> str:
    """把用户输入归一化为框架代码格式 ``XXXXXX.SH`` / ``XXXXXX.SZ``。

    支持的输入：
      - ``510300.SH`` / ``510300.sh``  → ``510300.SH``
      - ``510300``                     → ``510300.SH``（按前缀推断）
      - ``000001``                     → ``000001.SZ``

    Raises:
        ValueError: 输入为空、长度错误、含未知后缀或无法识别交易所时抛出。
    """
    if raw is None:
        raise ValueError("代码为空")
    s = str(raw).strip().upper()
    if not s:
        raise ValueError("代码为空")
    if "." in s:
        bare, _, suffix = s.partition(".")
        if suffix not in ("SH", "SZ", "BJ"):
            raise ValueError(f"未知交易所后缀: {raw}（应为 .SH、.SZ 或 .BJ）")
        if not bare.isdigit() or len(bare) != 6:
            raise ValueError(f"代码格式错误: {raw}（应为 6 位数字）")
        return f"{bare}.{suffix}"
    # 裸 6 位代码：按前缀推断
    if not s.isdigit() or len(s) != 6:
        raise ValueError(f"代码格式错误: {raw}（应为 6 位数字 + 可选 .SH/.SZ/.BJ 后缀）")
    if s.startswith(_SH_PREFIXES):
        return f"{s}.SH"
    if s.startswith(_SZ_PREFIXES):
        return f"{s}.SZ"
    if s.startswith(_BJ_PREFIXES):
        return f"{s}.BJ"
    raise ValueError(f"无法识别代码所属交易所: {raw}（请显式写明 .SH、.SZ 或 .BJ）")


def parse_universe(raw: str) -> List[str]:
    """把逗号/空格分隔的字符串解析为代码列表，并归一化每一项。"""
    if not raw:
        return []
    parts = [p for p in raw.replace(",", " ").split() if p]
    return [normalize_code(p) for p in parts]


def to_exchange_code(framework_code: str) -> str:
    """框架代码 → 表代码：``600000.SH`` → ``sh600000``。

    幂等：已是表格式时（``sh600000`` / ``sz000001``）原样返回。
    无法识别交易所时抛 ValueError（与原 maxcompute_source 的"默认 sh + WARNING"行为不同）。
    """
    if not framework_code:
        raise ValueError("股票代码为空")
    code = str(framework_code).strip()
    lower = code.lower()

    if lower.startswith(("sh", "sz", "bj")) and len(lower) == 8 and lower[2:].isdigit():
        return lower

    if "." in code:
        bare, _, suffix = code.partition(".")
        suffix = suffix.upper()
        if suffix == "SH":
            return f"sh{bare}"
        if suffix == "SZ":
            return f"sz{bare}"
        if suffix == "BJ":
            return f"bj{bare}"
        raise ValueError(f"未知交易所后缀: {framework_code}")

    bare = code
    if bare.isdigit() and len(bare) == 6:
        if bare.startswith(_SH_PREFIXES):
            return f"sh{bare}"
        if bare.startswith(_SZ_PREFIXES):
            return f"sz{bare}"
        if bare.startswith(_BJ_PREFIXES):
            return f"bj{bare}"
    raise ValueError(f"无法识别股票代码 {framework_code} 的交易所")


def _parse_date(date_str: str) -> _dt.date | None:
    """把 ``YYYYMMDD`` / ``YYYY-MM-DD`` / ``YYYY/MM/DD`` 解析为 ``date``。失败返回 None。"""
    if not date_str:
        return None
    s = str(date_str).strip().replace("-", "").replace("/", "")
    # 取前 8 位数字（兼容 YYYYMMDDHHMM 等更长格式）
    if len(s) >= 8 and s[:8].isdigit():
        try:
            return _dt.date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    return None


def price_limit_pct(code: str, current_date: str = "", list_date: str = "") -> float:
    """根据股票代码（按板块）返回涨跌停幅度（小数，如 0.10 表示 ±10%）。

    板块规则（A 股 2026 现行）：

    +------------------------+----------------------+--------+
    | 代码模式               | 板块                 | 涨跌幅 |
    +========================+======================+========+
    | ``60xxxx.SH``          | 沪市主板             | 0.10   |
    +------------------------+----------------------+--------+
    | ``000/001/002/003.SZ`` | 深市主板（含中小板） | 0.10   |
    +------------------------+----------------------+--------+
    | ``688/689xxxx.SH``     | 科创板               | 0.20   |
    +------------------------+----------------------+--------+
    | ``300/301xxxx.SZ``     | 创业板               | 0.20*  |
    +------------------------+----------------------+--------+
    | ``43/83/87/88/92xxxx`` | 北交所               | 0.30   |
    +------------------------+----------------------+--------+
    | ``51/56/58/11.SH``     | 沪 ETF/LOF/可转债    | 0.10   |
    +------------------------+----------------------+--------+
    | ``15/16xxxx.SZ``       | 深 ETF/LOF           | 0.10   |
    +------------------------+----------------------+--------+
    | 其他/异常输入          | fallback             | 0.10   |
    +------------------------+----------------------+--------+

    \\* 创业板按 ``current_date`` 切换：≥2020-08-24 为 ±20%，之前为 ±10%。

    新股上市首 ``_NEW_LISTING_NO_LIMIT_DAYS`` 个交易日无涨跌幅限制，返回 ``1.0``（即 ±100%，
    撮合层以 ``prev_close * 2.0`` 为上限，等效不限）。第 N+1 个交易日起恢复常规板块规则。

    Args:
        code: 框架代码（``XXXXXX.SH`` / ``XXXXXX.SZ`` / ``XXXXXX.BJ``）或裸 6 位代码。
        current_date: 当前回测日期；创业板切换 + 新股首日均依赖此值；缺省时按最新规则。
        list_date: 股票上市日期（``YYYYMMDD``）；传入时启用新股首日规则；缺省时跳过。

    Returns:
        涨跌停比例（小数）。异常输入返回 0.10 作为兜底。

    Note:
        本函数暂不支持以下规则（已在 TODO.md 中记录）：

        - ST / *ST 股票 ±5%：当前数据源无 ST 标签
    """
    if not code:
        return 0.10
    s = str(code).strip().upper()
    bare = s.partition(".")[0] if "." in s else s

    if not bare.isdigit() or len(bare) != 6:
        return 0.10

    # 新股首日：前 N 个交易日无涨跌幅限制（所有板块统一，2023 全面注册制后）
    if list_date and current_date:
        if _is_within_first_n_trading_days(current_date, list_date, _NEW_LISTING_NO_LIMIT_DAYS):
            return 1.0

    # 科创板：始终 ±20%
    if bare.startswith(("688", "689")):
        return 0.20

    # 创业板：按日期切换
    if bare.startswith(("300", "301")):
        parsed = _parse_date(current_date)
        if parsed is None:
            return 0.20  # 无日期信息时取最新规则
        return 0.20 if parsed >= _CHINEXT_REFORM_DATE else 0.10

    # 北交所：始终 ±30%
    if bare.startswith(_BJ_PREFIXES):
        return 0.30

    # 其他（主板、ETF/LOF/可转债）统一 ±10%
    return 0.10


def _is_within_first_n_trading_days(current: str, list_date: str, n: int) -> bool:
    """``current`` 是否在 ``list_date`` 起前 ``n`` 个交易日内（含 ``list_date`` 当天）。

    使用 TradingCalendar 统计闭区间 [list_date, current] 内的交易日数。
    若 current < list_date（防御性检查），返回 False。
    """
    cd = _parse_date(current)
    ld = _parse_date(list_date)
    if cd is None or ld is None or cd < ld:
        return False
    days = TradingCalendar.get_trading_days(list_date, current)
    return 1 <= len(days) <= n


def to_framework_code(exchange_code: str) -> str:
    """表代码 → 框架代码：``sh600000`` → ``600000.SH``，``bj832000`` → ``832000.BJ``。"""
    if not exchange_code:
        return exchange_code
    code = str(exchange_code).strip().lower()
    if code.startswith("sh") and len(code) == 8 and code[2:].isdigit():
        return f"{code[2:]}.SH"
    if code.startswith("sz") and len(code) == 8 and code[2:].isdigit():
        return f"{code[2:]}.SZ"
    if code.startswith("bj") and len(code) == 8 and code[2:].isdigit():
        return f"{code[2:]}.BJ"
    # 已是框架格式则原样返回
    if "." in code:
        return code.upper()
    raise ValueError(f"无法解析交易所代码: {exchange_code}")
