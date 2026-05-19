"""double_ma 策略包。

重导出主类，让 `strategy.double_ma.DoubleMAStrategy` 这个导入路径
与旧版本（strategy/double_ma.py 单文件）完全兼容。
"""

from strategy.double_ma.strategy import DoubleMAStrategy

__all__ = ["DoubleMAStrategy"]
