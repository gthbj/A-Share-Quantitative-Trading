"""utils.code 单元测试。"""

import pytest

from utils.code import (
    normalize_code,
    parse_universe,
    to_exchange_code,
    to_framework_code,
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


class TestToExchangeCode:
    def test_sh(self):
        assert to_exchange_code("600000.SH") == "sh600000"

    def test_sz(self):
        assert to_exchange_code("000001.SZ") == "sz000001"

    def test_already_exchange_format(self):
        # 幂等
        assert to_exchange_code("sh600000") == "sh600000"

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            to_exchange_code("510300.HK")


class TestToFrameworkCode:
    def test_sh(self):
        assert to_framework_code("sh600000") == "600000.SH"

    def test_sz(self):
        assert to_framework_code("sz000001") == "000001.SZ"

    def test_uppercase_input(self):
        assert to_framework_code("SH600000") == "600000.SH"
