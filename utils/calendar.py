"""A股交易日历工具。

提供交易日判断、交易日期序列生成等功能。
暂时使用简单实现，后续可接入 exchange_calendars 或 AKShare 的交易日历。
"""

from __future__ import annotations

import datetime
from typing import List, Optional


class TradingCalendar:
    """A股交易日历（简化版）。

    基于中国大陆公共假日规则进行近似判断，精确日历可后续接入 AKShare。
    """

    # 2020-2024 已知长假日期（除夕~初六、五一、国庆等），仅用于演示
    _KNOWN_HOLIDAYS: set[str] = {
        # 2020 春节
        "20200124", "20200125", "20200126", "20200127", "20200128", "20200129", "20200130",
        # 2020 五一
        "20200501", "20200502", "20200503", "20200504", "20200505",
        # 2020 国庆
        "20201001", "20201002", "20201003", "20201004", "20201005", "20201006", "20201007",
        # 2021 春节
        "20210211", "20210212", "20210213", "20210214", "20210215", "20210216", "20210217",
        # 2021 五一
        "20210501", "20210502", "20210503", "20210504", "20210505",
        # 2021 国庆
        "20211001", "20211002", "20211003", "20211004", "20211005", "20211006", "20211007",
        # 2022 春节
        "20220131", "20220201", "20220202", "20220203", "20220204", "20220205", "20220206",
        # 2022 五一
        "20220430", "20220501", "20220502", "20220503", "20220504",
        # 2022 国庆
        "20221001", "20221002", "20221003", "20221004", "20221005", "20221006", "20221007",
        # 2023 春节
        "20230121", "20230122", "20230123", "20230124", "20230125", "20230126", "20230127",
        # 2023 五一
        "20230429", "20230430", "20230501", "20230502", "20230503",
        # 2023 国庆
        "20230929", "20230930", "20231001", "20231002", "20231003", "20231004", "20231005", "20231006",
        # 2024 春节
        "20240209", "20240210", "20240211", "20240212", "20240213", "20240214", "20240215", "20240216", "20240217",
        # 2024 五一
        "20240501", "20240502", "20240503", "20240504", "20240505",
        # 2024 国庆
        "20241001", "20241002", "20241003", "20241004", "20241005", "20241006", "20241007",
    }

    @classmethod
    def is_trading_day(cls, date: datetime.date | str) -> bool:
        """判断给定日期是否为交易日。"""
        if isinstance(date, str):
            date = datetime.datetime.strptime(date, "%Y%m%d").date()
        if date.weekday() >= 5:  # 周六日
            return False
        if date.strftime("%Y%m%d") in cls._KNOWN_HOLIDAYS:
            return False
        return True

    @classmethod
    def get_trading_days(
        cls, start: str | datetime.date, end: str | datetime.date
    ) -> List[datetime.date]:
        """获取闭区间内的所有交易日。"""
        if isinstance(start, str):
            start = datetime.datetime.strptime(start, "%Y%m%d").date()
        if isinstance(end, str):
            end = datetime.datetime.strptime(end, "%Y%m%d").date()

        days: List[datetime.date] = []
        cur = start
        while cur <= end:
            if cls.is_trading_day(cur):
                days.append(cur)
            cur += datetime.timedelta(days=1)
        return days

    @classmethod
    def next_trading_day(cls, date: str | datetime.date) -> datetime.date:
        """获取下一个交易日。"""
        if isinstance(date, str):
            date = datetime.datetime.strptime(date, "%Y%m%d").date()
        nxt = date + datetime.timedelta(days=1)
        while not cls.is_trading_day(nxt):
            nxt += datetime.timedelta(days=1)
        return nxt

    @classmethod
    def prev_trading_day(cls, date: str | datetime.date) -> datetime.date:
        """获取上一个交易日。"""
        if isinstance(date, str):
            date = datetime.datetime.strptime(date, "%Y%m%d").date()
        prv = date - datetime.timedelta(days=1)
        while not cls.is_trading_day(prv):
            prv -= datetime.timedelta(days=1)
        return prv


# 全局单例（简化使用）
_calendar: Optional[TradingCalendar] = None


def get_trading_calendar() -> TradingCalendar:
    global _calendar
    if _calendar is None:
        _calendar = TradingCalendar()
    return _calendar
