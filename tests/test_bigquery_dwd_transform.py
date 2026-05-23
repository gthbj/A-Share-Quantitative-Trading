import pandas as pd
import pytest
import yaml

from gcs_to_bigquery.pipeline import (
    apply_field_mappings,
    get_code_column_config,
    normalize_security_code,
    resolve_source_column,
    validate_financial_date_policy,
)


@pytest.fixture()
def config():
    return yaml.safe_load(
        """
field_mappings:
  common:
    股票代码: equity_code
    证券代码: security_code
    基金代码: fund_code
    指数代码: index_code
    板块代码: board_code
    成分股票代码: equity_code
    日期: date_raw
    开盘: open_raw
    收盘: close_raw

  per_table:
    fact_equity_kline_1d:
      code_column: equity_code
      source_candidates: ["equity_code", "security_code", "股票代码", "证券代码", "代码"]
    fact_fund_kline_1d:
      code_column: fund_code
      source_candidates: ["fund_code", "security_code", "基金代码", "基金交易代码", "代码"]
    fact_index_kline_1d:
      code_column: index_code
      source_candidates: ["index_code", "security_code", "指数代码", "代码"]
    fact_board_kline_1d:
      code_column: board_code
      source_candidates: ["board_code", "板块代码", "代码"]
    fact_board_component_1d:
      code_columns: ["board_code", "equity_code"]
      board_source_candidates: ["board_code", "板块代码"]
      equity_source_candidates: ["equity_code", "security_code", "成分股票代码", "股票代码", "代码"]
    dim_security:
      code_column: security_code
      source_candidates: ["security_code", "证券代码", "股票代码", "基金代码", "指数代码", "代码"]

financial_date_policy:
  strict_visible_date: "announcement_date"
  report_period_is_not_visible_date: true
"""
    )


class TestNormalizeSecurityCode:
    def test_sh_prefix_6(self):
        assert normalize_security_code("510300") == "510300.SH"

    def test_sh_prefix_9(self):
        assert normalize_security_code("900901") == "900901.SH"

    def test_sz_prefix(self):
        assert normalize_security_code("000001") == "000001.SZ"

    def test_bj_prefix_43(self):
        assert normalize_security_code("430139") == "430139.BJ"

    def test_bj_prefix_83(self):
        assert normalize_security_code("830799") == "830799.BJ"

    def test_bj_prefix_87(self):
        assert normalize_security_code("870299") == "870299.BJ"

    def test_bj_prefix_88(self):
        assert normalize_security_code("880139") == "880139.BJ"

    def test_bj_prefix_92(self):
        assert normalize_security_code("920139") == "920139.BJ"

    def test_already_dot_format(self):
        assert normalize_security_code("000001.SZ") == "000001.SZ"

    def test_exchange_prefix_sh(self):
        assert normalize_security_code("SH000001") == "000001.SH"

    def test_exchange_prefix_sz(self):
        assert normalize_security_code("SZ000001") == "000001.SZ"

    def test_exchange_prefix_bj(self):
        assert normalize_security_code("BJ430139") == "430139.BJ"

    def test_underscore_format(self):
        assert normalize_security_code("000001_SZ") == "000001.SZ"

    def test_none(self):
        assert normalize_security_code(None) is None

    def test_empty(self):
        assert normalize_security_code("") is None

    def test_whitespace(self):
        assert normalize_security_code("  000001  ") == "000001.SZ"

    def test_lower_case(self):
        assert normalize_security_code("sh000001") == "000001.SH"


class TestResolveSourceColumn:
    def test_first_match(self):
        assert resolve_source_column(["security_code", "date"], ["security_code", "code"]) == "security_code"

    def test_fallback_match(self):
        assert resolve_source_column(["股票代码", "date"], ["security_code", "股票代码"]) == "股票代码"

    def test_no_match(self):
        assert resolve_source_column(["date", "open"], ["security_code", "code"]) is None

    def test_empty_candidates(self):
        assert resolve_source_column(["date"], []) is None


class TestGetCodeColumnConfig:
    def test_equity_table(self, config):
        result = get_code_column_config(config, "fact_equity_kline_1d")
        assert result["code_column"] == "equity_code"

    def test_dim_security(self, config):
        result = get_code_column_config(config, "dim_security")
        assert result["code_column"] == "security_code"

    def test_unknown_table(self, config):
        assert get_code_column_config(config, "nonexistent_table") is None


class TestApplyFieldMappings:
    def test_equity_code_field(self, config):
        df = pd.DataFrame({"security_code": ["000001.SZ", "600519.SH"], "open": [10.0, 20.0]})
        result = apply_field_mappings(config, "fact_equity_kline_1d", df)
        assert "equity_code" in result.columns
        assert "security_code" not in result.columns
        assert list(result["equity_code"]) == ["000001.SZ", "600519.SH"]

    def test_chinese_column_mapping(self, config):
        df = pd.DataFrame({"股票代码": ["000001", "600519"], "开盘": [10.0, 20.0]})
        result = apply_field_mappings(config, "fact_equity_kline_1d", df)
        assert "equity_code" in result.columns
        assert "open_raw" in result.columns
        assert list(result["equity_code"]) == ["000001.SZ", "600519.SH"]

    def test_dim_security_keeps_security_code(self, config):
        df = pd.DataFrame({"证券代码": ["000001", "600519"], "名称": ["A", "B"]})
        result = apply_field_mappings(config, "dim_security", df)
        assert "security_code" in result.columns
        assert "equity_code" not in result.columns

    def test_board_component_dual_code(self, config):
        df = pd.DataFrame({"板块代码": ["000001"], "成分股票代码": ["000002"], "date": ["20240101"]})
        result = apply_field_mappings(config, "fact_board_component_1d", df)
        assert "board_code" in result.columns
        assert "equity_code" in result.columns

    def test_bj_code_normalized(self, config):
        df = pd.DataFrame({"security_code": ["430139", "830799"], "open": [1.0, 2.0]})
        result = apply_field_mappings(config, "fact_equity_kline_1d", df)
        assert list(result["equity_code"]) == ["430139.BJ", "830799.BJ"]

    def test_fund_table(self, config):
        df = pd.DataFrame({"基金代码": ["510300", "159915"], "open": [1.0, 2.0]})
        result = apply_field_mappings(config, "fact_fund_kline_1d", df)
        assert "fund_code" in result.columns
        assert list(result["fund_code"]) == ["510300.SH", "159915.SZ"]

    def test_index_table(self, config):
        df = pd.DataFrame({"指数代码": ["000001", "399001"], "open": [1.0, 2.0]})
        result = apply_field_mappings(config, "fact_index_kline_1d", df)
        assert "index_code" in result.columns
        assert list(result["index_code"]) == ["000001.SZ", "399001.SZ"]


class TestFinancialDatePolicy:
    def test_report_period_preserved(self, config):
        df = pd.DataFrame({
            "report_period_raw": ["20240331"],
            "announcement_date_raw": ["20240430"],
        })
        result = validate_financial_date_policy(config, df, "fact_financial_indicator")
        assert "report_period_raw" in result.columns
        assert "announcement_date_raw" in result.columns

    def test_missing_announcement_date_filled(self, config):
        df = pd.DataFrame({"report_period_raw": ["20240331"]})
        result = validate_financial_date_policy(config, df, "fact_financial_indicator")
        assert "announcement_date_raw" in result.columns

    def test_no_report_period(self, config):
        df = pd.DataFrame({"date": ["20240101"]})
        result = validate_financial_date_policy(config, df, "fact_equity_kline_1d")
        assert "announcement_date_raw" not in result.columns
