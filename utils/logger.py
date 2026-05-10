"""日志配置工具。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional


def setup_logging(
    level: str = "INFO",
    log_file: Optional[str] = None,
    fmt: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
) -> None:
    """配置全局日志。

    Args:
        level: 日志级别，如 DEBUG / INFO / WARNING / ERROR。
        log_file: 日志文件路径，若为 None 则仅输出到控制台。
        fmt: 日志格式字符串。
    """
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))

    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=fmt,
        handlers=handlers,
        force=True,
    )


def get_logger(name: str) -> logging.Logger:
    """获取指定名称的 Logger。"""
    return logging.getLogger(name)
