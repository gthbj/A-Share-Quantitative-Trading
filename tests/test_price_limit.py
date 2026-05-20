"""测试 utils.code.price_limit_pct：板块涨跌停规则。"""

from __future__ import annotations

import pytest

from utils.code import price_limit_pct


class TestMainBoard:
    """主板：±10%"""

    def test_sh_main_board(self):
        assert price_limit_pct("600000.SH") == 0.10
        assert price_limit_pct("601318.SH") == 0.10
        assert price_limit_pct("603288.SH") == 0.10
        assert price_limit_pct("605499.SH") == 0.10

    def test_sz_main_board(self):
        assert price_limit_pct("000001.SZ") == 0.10
        assert price_limit_pct("001872.SZ") == 0.10
        assert price_limit_pct("002415.SZ") == 0.10  # 原中小板归主板
        assert price_limit_pct("003816.SZ") == 0.10  # 注册制后新代码段

    def test_bare_code(self):
        # 不带后缀但前缀清晰：仍能正确识别
        assert price_limit_pct("600000") == 0.10
        assert price_limit_pct("000001") == 0.10


class TestStarMarket:
    """科创板：±20%"""

    def test_star_market(self):
        assert price_limit_pct("688981.SH") == 0.20
        assert price_limit_pct("688012.SH") == 0.20
        assert price_limit_pct("689009.SH") == 0.20  # CDR


class TestChiNext:
    """创业板：2020-08-24 起 ±20%，之前 ±10%"""

    def test_after_reform(self):
        assert price_limit_pct("300750.SZ", "20200824") == 0.20  # 改革当天
        assert price_limit_pct("300750.SZ", "20240101") == 0.20
        assert price_limit_pct("301236.SZ", "20240101") == 0.20

    def test_before_reform(self):
        assert price_limit_pct("300750.SZ", "20200823") == 0.10
        assert price_limit_pct("300750.SZ", "20200101") == 0.10
        assert price_limit_pct("300750.SZ", "20150615") == 0.10

    def test_without_date_uses_latest(self):
        # 无日期：默认采用最新规则（±20%）
        assert price_limit_pct("300750.SZ") == 0.20
        assert price_limit_pct("300750.SZ", "") == 0.20

    def test_date_format_variants(self):
        # YYYY-MM-DD / YYYY/MM/DD 都应被识别
        assert price_limit_pct("300750.SZ", "2020-08-24") == 0.20
        assert price_limit_pct("300750.SZ", "2020/08/24") == 0.20
        assert price_limit_pct("300750.SZ", "2020-08-23") == 0.10
        # YYYYMMDDHHMM（分钟级时间戳）
        assert price_limit_pct("300750.SZ", "202008240930") == 0.20
        assert price_limit_pct("300750.SZ", "202008230930") == 0.10

    def test_invalid_date_falls_back_to_latest(self):
        # 解析失败按最新规则
        assert price_limit_pct("300750.SZ", "invalid") == 0.20
        assert price_limit_pct("300750.SZ", "20991340") == 0.20  # 非法月份


class TestETFAndConvertible:
    """ETF / LOF / 可转债：±10%"""

    def test_sh_etf(self):
        assert price_limit_pct("510300.SH") == 0.10  # 沪深300 ETF
        assert price_limit_pct("588000.SH") == 0.10  # 科创50 ETF
        assert price_limit_pct("563300.SH") == 0.10  # 主题 ETF

    def test_sz_etf(self):
        assert price_limit_pct("159915.SZ") == 0.10  # 创业板 ETF
        assert price_limit_pct("161725.SZ") == 0.10  # LOF

    def test_convertible_bond(self):
        assert price_limit_pct("113008.SH") == 0.10  # 可转债


class TestFallback:
    """异常输入：fallback 到 ±10%（不抛异常）"""

    def test_empty_string(self):
        assert price_limit_pct("") == 0.10

    def test_none(self):
        assert price_limit_pct(None) == 0.10  # type: ignore[arg-type]

    def test_invalid_format(self):
        assert price_limit_pct("abc") == 0.10
        assert price_limit_pct("12345") == 0.10  # 长度不对
        assert price_limit_pct("1234567") == 0.10

    def test_unknown_prefix(self):
        # 北交所代码（前缀 8/4）：本期不支持，fallback 到 0.10
        # 等北交所正式接入后再细分（TODO）
        assert price_limit_pct("832000.BJ") == 0.10
        assert price_limit_pct("832000") == 0.10
