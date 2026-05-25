"""utils.code 单元测试。"""

import pytest

from utils.code import (
    normalize_code,
    parse_universe,
)


class TestNormalizeCode:
    def test_with_uppercase_suffix(self):
        assert normalize_code("510300.SH") == "510300.SH"

    def test_with_lowercase_suffix(self):
        assert normalize_code("510300.sh") == "510300.SH"

    def test_bare_sh_prefix(self):
        # 51 是 ETF 前缀（沪市）
        assert normalize_code("510300") == "510300.SH"
        # 60 是沪市主板
        assert normalize_code("600000") == "600000.SH"
        # 68 是科创板
        assert normalize_code("688008") == "688008.SH"

    def test_bare_sz_prefix(self):
        # 00 深市主板
        assert normalize_code("000001") == "000001.SZ"
        # 30 创业板
        assert normalize_code("300750") == "300750.SZ"
        # 15 ETF
        assert normalize_code("159919") == "159919.SZ"

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            normalize_code("")

    def test_invalid_suffix_raises(self):
        with pytest.raises(ValueError):
            normalize_code("510300.HK")

    def test_unknown_prefix_raises(self):
        with pytest.raises(ValueError):
            normalize_code("999999")

    def test_wrong_length_raises(self):
        with pytest.raises(ValueError):
            normalize_code("12345")


class TestParseUniverse:
    def test_single(self):
        assert parse_universe("510300.SH") == ["510300.SH"]

    def test_comma_separated(self):
        assert parse_universe("510300.SH,510500.SH") == ["510300.SH", "510500.SH"]

    def test_space_separated(self):
        assert parse_universe("510300.SH 510500.SH") == ["510300.SH", "510500.SH"]

    def test_mixed_separators_with_normalization(self):
        # 含裸代码与带后缀混合
        assert parse_universe("510300,000001 600000.sh") == [
            "510300.SH", "000001.SZ", "600000.SH",
        ]

    def test_empty(self):
        assert parse_universe("") == []


class TestBeiJingExchange:
    """北交所代码归一化与互转（PRD_20260520_10）。"""

    # --- normalize_code ---

    def test_normalize_with_bj_suffix_upper(self):
        assert normalize_code("832000.BJ") == "832000.BJ"

    def test_normalize_with_bj_suffix_lower(self):
        assert normalize_code("832000.bj") == "832000.BJ"

    def test_normalize_bare_83_prefix(self):
        # 普通北交所代码（83 字头）
        assert normalize_code("832000") == "832000.BJ"

    def test_normalize_bare_43_prefix(self):
        # 老三板（43 字头）
        assert normalize_code("430718") == "430718.BJ"

    def test_normalize_bare_87_prefix(self):
        assert normalize_code("873726") == "873726.BJ"

    def test_normalize_bare_88_prefix(self):
        assert normalize_code("880188") == "880188.BJ"

    def test_normalize_bare_92_prefix(self):
        # 精选层（92 字头）
        assert normalize_code("920001") == "920001.BJ"

    def test_normalize_invalid_bj_suffix_raises(self):
        with pytest.raises(ValueError):
            normalize_code("832000.XY")
