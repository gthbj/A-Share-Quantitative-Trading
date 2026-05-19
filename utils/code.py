"""股票代码归一化与映射工具。

统一框架内的代码格式规范，供 CLI / data_layer / engine / strategy 共享。

约定：
  - 框架代码：``XXXXXX.SH`` / ``XXXXXX.SZ``（大写后缀）
  - 表代码：``shXXXXXX`` / ``szXXXXXX``（小写前缀，仅部分 MaxCompute 表内使用）
  - 裸 6 位代码：按前缀推断交易所；无法识别则抛 ValueError
"""

from __future__ import annotations

from typing import List

# 上交所代码前缀（沪市主板 / 科创板 / ETF / LOF / 转债）
_SH_PREFIXES = ("60", "68", "51", "56", "58", "11")
# 深交所代码前缀（深市主板 / 创业板 / ETF / LOF）
_SZ_PREFIXES = ("00", "30", "15", "16")


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
        if suffix not in ("SH", "SZ"):
            raise ValueError(f"未知交易所后缀: {raw}（应为 .SH 或 .SZ）")
        if not bare.isdigit() or len(bare) != 6:
            raise ValueError(f"代码格式错误: {raw}（应为 6 位数字）")
        return f"{bare}.{suffix}"
    # 裸 6 位代码：按前缀推断
    if not s.isdigit() or len(s) != 6:
        raise ValueError(f"代码格式错误: {raw}（应为 6 位数字 + 可选 .SH/.SZ 后缀）")
    if s.startswith(_SH_PREFIXES):
        return f"{s}.SH"
    if s.startswith(_SZ_PREFIXES):
        return f"{s}.SZ"
    raise ValueError(f"无法识别代码所属交易所: {raw}（请显式写明 .SH 或 .SZ）")


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

    if lower.startswith(("sh", "sz")) and len(lower) == 8 and lower[2:].isdigit():
        return lower

    if "." in code:
        bare, _, suffix = code.partition(".")
        suffix = suffix.upper()
        if suffix == "SH":
            return f"sh{bare}"
        if suffix == "SZ":
            return f"sz{bare}"
        raise ValueError(f"未知交易所后缀: {framework_code}")

    bare = code
    if bare.isdigit() and len(bare) == 6:
        if bare.startswith(_SH_PREFIXES):
            return f"sh{bare}"
        if bare.startswith(_SZ_PREFIXES):
            return f"sz{bare}"
    raise ValueError(f"无法识别股票代码 {framework_code} 的交易所")


def to_framework_code(exchange_code: str) -> str:
    """表代码 → 框架代码：``sh600000`` → ``600000.SH``。"""
    if not exchange_code:
        return exchange_code
    code = str(exchange_code).strip().lower()
    if code.startswith("sh") and len(code) == 8 and code[2:].isdigit():
        return f"{code[2:]}.SH"
    if code.startswith("sz") and len(code) == 8 and code[2:].isdigit():
        return f"{code[2:]}.SZ"
    # 已是框架格式则原样返回
    if "." in code:
        return code.upper()
    raise ValueError(f"无法解析交易所代码: {exchange_code}")
