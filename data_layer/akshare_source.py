"""AKShare 数据源实现。

利用 AKShare 免费接口获取 A 股行情，并自动缓存到本地 LocalStorage。
"""

from __future__ import annotations

from typing import List, Optional

import pandas as pd

from .base_data_source import BaseDataSource
from .local_storage import LocalStorage
from utils.logger import get_logger

logger = get_logger(__name__)


class AKShareDataSource(BaseDataSource):
    """基于 AKShare 的 A 股数据源。

    首次请求时会调用 AKShare API 拉取数据并写入本地缓存；
    后续请求优先读取本地缓存，缺失部分再增量拉取。
    """

    def __init__(
        self,
        storage: Optional[LocalStorage] = None,
        use_cache: bool = True,
    ) -> None:
        self.storage = storage or LocalStorage()
        self.use_cache = use_cache
        self._ak = None  # 懒加载

    def _get_ak(self):
        if self._ak is None:
            import akshare as ak

            self._ak = ak
        return self._ak

    def _normalize_code(self, code: str) -> str:
        """统一代码格式，去除后缀用于 AKShare 查询。"""
        return code.split(".")[0]

    def _detect_asset_type(self, code: str) -> str:
        """根据代码前缀判断资产类型：stock / etf / index。

        规则：
        - ETF：前缀为 15, 16, 51, 56, 58, 59
        - 指数：前缀为 000, 399, 88 且后缀为 .SH 或 .SZ
        - 个股：其他
        """
        norm = self._normalize_code(code)
        if norm.startswith(("15", "16", "51", "56", "58", "59")):
            return "etf"
        if norm.startswith(("000", "399", "88")):
            return "index"
        return "stock"

    def _fetch_stock_bars(
        self,
        ak,
        norm_code: str,
        ak_period: str,
        start_date: str,
        end_date: str,
        adjust: str,
    ) -> pd.DataFrame:
        """获取个股行情。"""
        try:
            return ak.stock_zh_a_hist(
                symbol=norm_code,
                period=ak_period,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
        except Exception:
            return pd.DataFrame()

    def _fetch_etf_bars(
        self,
        ak,
        norm_code: str,
        ak_period: str,
        start_date: str,
        end_date: str,
        adjust: str,
    ) -> pd.DataFrame:
        """获取 ETF 行情。"""
        try:
            return ak.fund_etf_hist_em(
                symbol=norm_code,
                period=ak_period,
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
        except Exception:
            return pd.DataFrame()

    def _fetch_index_bars(
        self,
        ak,
        norm_code: str,
        ak_period: str,
        start_date: str,
        end_date: str,
        adjust: str,
    ) -> pd.DataFrame:
        """获取指数行情。

        注意：index_zh_a_hist 不支持 adjust 参数，若传入则忽略。
        """
        try:
            return ak.index_zh_a_hist(
                symbol=norm_code,
                period=ak_period,
                start_date=start_date,
                end_date=end_date,
            )
        except Exception:
            return pd.DataFrame()

    def _standardize_df(self, df: pd.DataFrame, norm_code: str, period: str) -> pd.DataFrame:
        """将 AKShare 返回的 DataFrame 标准化为统一格式。"""
        if df is None or df.empty:
            return pd.DataFrame()

        rename_map = {
            "日期": "date",
            "时间": "date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            "成交额": "amount",
        }
        df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
        df["code"] = norm_code

        # 分钟级数据时间格式处理
        if period != "daily":
            df["date"] = df["date"].astype(str).str.replace("-", "").str.replace(":", "").str.replace(" ", "")
            df["date"] = df["date"].str.slice(0, 12)
        else:
            df["date"] = df["date"].astype(str).str.replace("-", "")

        # 确保返回的列与个股一致
        cols = ["code", "date", "open", "high", "low", "close", "volume", "amount"]
        available_cols = [c for c in cols if c in df.columns]
        return df[available_cols].copy()

    def get_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
        period: str = "daily",
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        ak = self._get_ak()
        norm_code = self._normalize_code(code)
        ak_period = self._map_period(period)
        asset_type = self._detect_asset_type(code)

        # 1. 尝试读本地缓存
        if self.use_cache:
            cached = self.storage.load_bars(norm_code, start_date, end_date, period=period)
            if not cached.empty:
                return cached

        # 2. 根据资产类型调用对应接口
        df = pd.DataFrame()
        if asset_type == "etf":
            df = self._fetch_etf_bars(ak, norm_code, ak_period, start_date, end_date, adjust)
        elif asset_type == "index":
            df = self._fetch_index_bars(ak, norm_code, ak_period, start_date, end_date, adjust)
        else:
            df = self._fetch_stock_bars(ak, norm_code, ak_period, start_date, end_date, adjust)

        # 3. 分钟级 fallback（仅个股/ETF）
        if (df is None or df.empty) and period != "daily" and asset_type != "index":
            logger.warning(
                f"AKShare 分钟数据获取失败 ({code}, {period})，"
                f"fallback 到基于日K的模拟分钟数据（仅用于框架逻辑验证，非真实行情）"
            )
            return self._generate_mock_intraday_bars(
                norm_code, start_date, end_date, period, adjust
            )

        df = self._standardize_df(df, norm_code, period)
        if df.empty:
            return pd.DataFrame()

        # 4. 写缓存
        if self.use_cache:
            self.storage.save_bars(norm_code, df, period=period)

        return df

    def _generate_mock_intraday_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
        period: str,
        adjust: str,
    ) -> pd.DataFrame:
        """基于日K数据生成模拟分钟级 Bar（仅用于框架逻辑验证）。

        生成规则：
        - 获取真实日K（open/high/low/close/volume/amount）
        - 对每一天，生成 N 根分钟 Bar（1min=240, 5min=48, ...）
        - 价格从 open 随机游走到 close，约束在 [low, high]
        - 成交量与成交额均分
        - 时间戳按 A 股交易时段排布：9:30~11:30, 13:00~15:00
        """
        import numpy as np

        ak = self._get_ak()
        bars_per_day = {"1min": 240, "5min": 48, "15min": 16, "30min": 8, "60min": 4}.get(period, 240)
        interval_minutes = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "60min": 60}.get(period, 1)

        # 获取日K（通过 get_bars 自动路由到对应接口，支持 ETF/指数）
        daily_df = self.get_bars(code, start_date, end_date, period="daily", adjust=adjust)
        if daily_df.empty:
            return pd.DataFrame()

        # 确保列名已标准化（get_bars 返回的已经是标准格式）
        daily_df = daily_df.copy()

        all_bars = []
        rng = np.random.RandomState(42)  # 固定种子，保证可复现

        for _, row in daily_df.iterrows():
            date_str = str(row["date"])
            open_p = float(row["open"])
            high_p = float(row["high"])
            low_p = float(row["low"])
            close_p = float(row["close"])
            vol = float(row["volume"])
            amt = float(row["amount"])

            # 生成随机游走路径：起点 open，终点 close
            steps = rng.randn(bars_per_day)
            walk = np.cumsum(steps)
            # 标准化：起点 0，终点 close - open
            walk = walk - walk[0]
            if abs(walk[-1]) < 1e-9:
                walk[-1] = 1.0
            walk = walk / walk[-1] * (close_p - open_p)
            prices = open_p + walk

            # 约束在 [low, high] 内
            prices = np.clip(prices, low_p, high_p)
            prices[0] = open_p
            prices[-1] = close_p

            # 生成交易时间列表
            times = []
            t = 930
            count = 0
            while count < bars_per_day:
                # 跳过 11:31~12:59 的午休
                if t > 1130 and t < 1300:
                    t = 1300
                hh = t // 100
                mm = t % 100
                if mm >= 60:
                    hh += 1
                    mm = 0
                    t = hh * 100 + mm
                    continue
                if hh >= 15 and mm > 0:
                    break
                times.append(f"{date_str}{t:04d}")
                t += interval_minutes
                count += 1

            # 补齐（若因午休逻辑导致数量不足，重复最后一根）
            while len(times) < bars_per_day:
                times.append(times[-1] if times else f"{date_str}1500")

            for i in range(bars_per_day):
                bar_open = prices[i]
                bar_close = prices[i + 1] if i + 1 < bars_per_day else prices[i]
                bar_high = max(bar_open, bar_close) + rng.uniform(0, (high_p - low_p) * 0.02)
                bar_low = min(bar_open, bar_close) - rng.uniform(0, (high_p - low_p) * 0.02)
                bar_high = min(bar_high, high_p)
                bar_low = max(bar_low, low_p)

                all_bars.append({
                    "code": code,
                    "date": times[i],
                    "open": round(bar_open, 2),
                    "high": round(bar_high, 2),
                    "low": round(bar_low, 2),
                    "close": round(bar_close, 2),
                    "volume": round(vol / bars_per_day, 0),
                    "amount": round(amt / bars_per_day, 2),
                })

        df = pd.DataFrame(all_bars)
        if self.use_cache:
            self.storage.save_bars(code, df, period=period)
        return df

    def _map_period(self, period: str) -> str:
        """将框架周期标识映射为 AKShare 接口所需的 period 参数。"""
        mapping = {
            "daily": "daily",
            "1min": "1",
            "5min": "5",
            "15min": "15",
            "30min": "30",
            "60min": "60",
        }
        return mapping.get(period, "daily")

    def get_stock_list(self) -> pd.DataFrame:
        cached = self.storage.load_stock_list()
        if not cached.empty:
            return cached

        ak = self._get_ak()
        df = ak.stock_zh_a_spot_em()
        # 取关键列
        cols = ["代码", "名称", "所属行业"]
        df = df[[c for c in cols if c in df.columns]].copy()
        df.columns = ["code", "name", "industry"]
        self.storage.save_stock_list(df)
        return df

    def get_index_constituents(self, index_code: str) -> List[str]:
        """获取指数成分股，目前仅支持部分常见指数。"""
        ak = self._get_ak()
        norm = index_code.split(".")[0]

        try:
            if norm == "000300":
                df = ak.index_stock_cons_weight_csindex(symbol="000300")
            elif norm == "000905":
                df = ak.index_stock_cons_weight_csindex(symbol="000905")
            elif norm == "000001":
                df = ak.index_stock_cons_weight_csindex(symbol="000001")
            else:
                return []
        except Exception:
            return []

        if "成分券代码" in df.columns:
            return df["成分券代码"].astype(str).tolist()
        if "code" in df.columns:
            return df["code"].astype(str).tolist()
        return []
