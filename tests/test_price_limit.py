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


class TestBeiJingExchange:
    """北交所：±30%（PRD_20260520_10）"""

    def test_bj_83_prefix(self):
        assert price_limit_pct("832000.BJ") == 0.30

    def test_bj_43_prefix(self):
        assert price_limit_pct("430718.BJ") == 0.30

    def test_bj_92_prefix(self):
        assert price_limit_pct("920001.BJ") == 0.30

    def test_bj_bare_code(self):
        # 裸代码前缀推断
        assert price_limit_pct("832000") == 0.30
        assert price_limit_pct("430718") == 0.30


class TestNewListing:
    """新股上市首 5 个交易日无涨跌幅限制，第 6 日起恢复常规规则（PRD_20260520_10）。

    基准日期序列（20240105 起的交易日）：
      day1=20240105(Fri) day2=20240108(Mon) day3=20240109(Tue)
      day4=20240110(Wed) day5=20240111(Thu) day6=20240112(Fri)
    """

    def test_star_market_first_day(self):
        # 科创板新股：上市当天
        assert price_limit_pct("688999.SH", "20240105", "20240105") == 1.0

    def test_star_market_fifth_day(self):
        # 第 5 个交易日仍无限制
        assert price_limit_pct("688999.SH", "20240111", "20240105") == 1.0

    def test_star_market_sixth_day_resumes(self):
        # 第 6 个交易日起恢复科创板 ±20%
        assert price_limit_pct("688999.SH", "20240112", "20240105") == 0.20

    def test_main_board_new_listing(self):
        # 主板新股首日也无限制
        assert price_limit_pct("600000.SH", "20240108", "20240105") == 1.0

    def test_bj_new_listing(self):
        # 北交所新股首日也无限制
        assert price_limit_pct("832000.BJ", "20240108", "20240105") == 1.0

    def test_no_list_date_uses_normal_rules(self):
        # 不传 list_date → 走常规规则
        assert price_limit_pct("300750.SZ", "20240101", "") == 0.20

    def test_current_before_list_date_uses_normal_rules(self):
        # current_date < list_date → 防御性退回常规规则
        assert price_limit_pct("688999.SH", "20240104", "20240105") == 0.20

    def test_only_current_date_without_list_date(self):
        # 仅传 current_date，不传 list_date → 常规科创板 ±20%
        assert price_limit_pct("688999.SH", "20240105") == 0.20


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
        # 真正无法识别的前缀（非沪深北）→ fallback 0.10
        assert price_limit_pct("999999") == 0.10
        assert price_limit_pct("700000") == 0.10
