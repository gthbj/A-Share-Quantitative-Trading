"""A股交易日历工具。

优先调用 ``chinese_calendar``（覆盖法定假日、调休补班，每年初更新版本）；
若该库未安装或日期超出其覆盖范围，则退化到"非周末"启发式判断并打 WARNING。

注：``chinese_calendar`` 是依赖库，每年初需要 ``pip install -U chinese-calendar`` 才能拿到新一年数据。
"""

from __future__ import annotations

import datetime
import warnings
from typing import List, Optional

try:
    import chinese_calendar as _cc
    _HAS_CC = True
except ImportError:  # pragma: no cover
    _cc = None
    _HAS_CC = False


# Fallback：硬编码 2020-2024 长假，保持与旧实现兼容
_FALLBACK_HOLIDAYS: set[str] = {
    # 2020 春节
    "20200124", "20200125", "20200126", "20200127", "20200128", "20200129", "20200130",
    "20200501", "20200502", "20200503", "20200504", "20200505",
    "20201001", "20201002", "20201003", "20201004", "20201005", "20201006", "20201007",
    "20210211", "20210212", "20210213", "20210214", "20210215", "20210216", "20210217",
    "20210501", "20210502", "20210503", "20210504", "20210505",
    "20211001", "20211002", "20211003", "20211004", "20211005", "20211006", "20211007",
    "20220131", "20220201", "20220202", "20220203", "20220204", "20220205", "20220206",
    "20220430", "20220501", "20220502", "20220503", "20220504",
    "20221001", "20221002", "20221003", "20221004", "20221005", "20221006", "20221007",
    "20230121", "20230122", "20230123", "20230124", "20230125", "20230126", "20230127",
    "20230429", "20230430", "20230501", "20230502", "20230503",
    "20230929", "20230930", "20231001", "20231002", "20231003", "20231004", "20231005", "20231006",
    "20240209", "20240210", "20240211", "20240212", "20240213", "20240214", "20240215", "20240216", "20240217",
    "20240501", "20240502", "20240503", "20240504", "20240505",
    "20241001", "20241002", "20241003", "20241004", "20241005", "20241006", "20241007",
}

_WARNED_FALLBACK = False


def _warn_fallback_once(reason: str) -> None:
    global _WARNED_FALLBACK
    if not _WARNED_FALLBACK:
        warnings.warn(
            f"交易日历降级为简化判断（仅排除周末 + 硬编码 2020-2024 假期）。原因：{reason}。"
            f"请确保已 `pip install -U chinese-calendar` 以使用精确日历。"
        )
        _WARNED_FALLBACK = True


def _is_workday_via_cc(d: datetime.date) -> Optional[bool]:
    """通过 chinese_calendar 判断是否工作日；超出覆盖范围或库缺失时返回 None。"""
    if not _HAS_CC:
        return None
    try:
        return _cc.is_workday(d)
    except (NotImplementedError, KeyError, ValueError):
        # chinese_calendar 对超出范围的年份会抛 NotImplementedError
        return None


def _is_workday_fallback(d: datetime.date) -> bool:
    """启发式判断：非周末 && 非硬编码假期。"""
    if d.weekday() >= 5:
        return False
    return d.strftime("%Y%m%d") not in _FALLBACK_HOLIDAYS


class TradingCalendar:
    """A股交易日历。

    判断顺序：``chinese_calendar.is_workday`` → fallback（非周末 + 硬编码假期）。
    """

    @classmethod
    def is_trading_day(cls, date: datetime.date | str) -> bool:
        """判断给定日期是否为 A 股交易日。"""
        if isinstance(date, str):
            date = datetime.datetime.strptime(date, "%Y%m%d").date()

        cc_result = _is_workday_via_cc(date)
        if cc_result is not None:
            # chinese_calendar 的 is_workday 已经处理了调休补班，但仍需排除周末调休日
            # A 股交易日 = 工作日（chinese_calendar 中工作日已涵盖周一到周五减去假期 + 调休加班的周末）
            # 注：chinese_calendar 把"周六补班"标为 workday=True，但 A 股**不**在这种补班日开市
            #     所以需要叠加"非周末"的二次过滤
            if date.weekday() >= 5:
                return False
            return cc_result

        # fallback
        _warn_fallback_once(f"chinese_calendar 未覆盖 {date.isoformat()}")
        return _is_workday_fallback(date)

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
