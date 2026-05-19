"""回测结果可视化：收益曲线、回撤、持仓分布等。"""

from __future__ import annotations

import platform
import warnings
from pathlib import Path
from typing import Optional

import matplotlib
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import pandas as pd

# 非交互环境使用 Agg 后端
matplotlib.use("Agg")


def _setup_chinese_font() -> None:
    """探测系统中可用的中文字体并配置 matplotlib，避免 CJK 字符渲染为方框。

    探测顺序按系统优先级排列；都找不到时仅 WARNING，不影响绘图。
    """
    system = platform.system()
    candidates = {
        "Darwin": ["PingFang SC", "Heiti TC", "STHeiti", "Arial Unicode MS"],
        "Linux": ["Noto Sans CJK SC", "WenQuanYi Zen Hei", "WenQuanYi Micro Hei"],
        "Windows": ["Microsoft YaHei", "SimHei", "SimSun"],
    }.get(system, [])

    available = {f.name for f in fm.fontManager.ttflist}
    for name in candidates:
        if name in available:
            plt.rcParams["font.sans-serif"] = [name] + plt.rcParams.get("font.sans-serif", [])
            plt.rcParams["axes.unicode_minus"] = False  # 避免负号渲染问题
            return
    warnings.warn(
        f"未在 {system} 系统中找到可用的中文字体，图表中文可能渲染为方框。"
        f"可尝试安装 Noto Sans CJK SC / PingFang SC / Microsoft YaHei 等字体。"
    )


_setup_chinese_font()


class Plotter:
    """回测图表绘制器。"""

    def __init__(self, figsize: tuple = (12, 6)) -> None:
        self.figsize = figsize

    def plot_cumulative_returns(
        self,
        nav_df: pd.DataFrame,
        benchmark_df: Optional[pd.DataFrame] = None,
        save_path: Optional[str] = None,
    ) -> None:
        """绘制策略与基准累计收益对比图。"""
        fig, ax = plt.subplots(figsize=self.figsize)

        strategy_cum = (1 + nav_df["returns"].fillna(0)).cumprod() - 1
        ax.plot(strategy_cum.index, strategy_cum * 100, label="策略", linewidth=1.5)

        if benchmark_df is not None and not benchmark_df.empty and "close" in benchmark_df.columns:
            bench = benchmark_df["close"]
            bench_cum = (bench / bench.iloc[0] - 1) * 100
            ax.plot(bench_cum.index, bench_cum, label="基准", linewidth=1.5, linestyle="--")

        ax.set_title("累计收益率对比 (%)")
        ax.set_xlabel("日期")
        ax.set_ylabel("收益率 (%)")
        ax.legend()
        ax.grid(True, alpha=0.3)
        plt.tight_layout()
        if save_path:
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            plt.savefig(save_path, dpi=150)
        plt.close(fig)

    def plot_drawdown(
        self,
        nav_df: pd.DataFrame,
        save_path: Optional[str] = None,
    ) -> None:
        """绘制回撤曲线。"""
        fig, ax = plt.subplots(figsize=self.figsize)
        nav = nav_df["nav"]
        cummax = nav.cummax()
        drawdown = (nav - cummax) / cummax * 100
        ax.fill_between(drawdown.index, drawdown, 0, color="red", alpha=0.3)
        ax.plot(drawdown.index, drawdown, color="red", linewidth=1)
        ax.set_title("回撤曲线 (%)")
        ax.set_xlabel("日期")
        ax.set_ylabel("回撤 (%)")
        ax.grid(True, alpha=0.3)
        plt.tight_layout()
        if save_path:
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            plt.savefig(save_path, dpi=150)
        plt.close(fig)

    def plot_monthly_returns(
        self,
        nav_df: pd.DataFrame,
        save_path: Optional[str] = None,
    ) -> None:
        """绘制月度收益热力图。"""
        returns = nav_df["returns"].fillna(0)
        # pandas < 2.2 用 "M"，>= 2.2 改为 "ME"（Month End）
        import pandas as pd
        _me_freq = "ME" if pd.__version__ >= "2.2" else "M"
        monthly = returns.resample(_me_freq).apply(lambda x: (1 + x).prod() - 1) * 100
        monthly.index = monthly.index.to_period("M")

        # 转为 pivot 表 (year x month)
        df = monthly.to_frame(name="ret")
        df["year"] = df.index.year
        df["month"] = df.index.month
        pivot = df.pivot(index="year", columns="month", values="ret")

        fig, ax = plt.subplots(figsize=(12, max(4, len(pivot) * 0.6)))
        cmap = plt.cm.RdYlGn
        im = ax.imshow(pivot.values, cmap=cmap, aspect="auto")

        ax.set_xticks(range(len(pivot.columns)))
        ax.set_xticklabels(pivot.columns)
        ax.set_yticks(range(len(pivot.index)))
        ax.set_yticklabels(pivot.index)

        for i in range(len(pivot.index)):
            for j in range(len(pivot.columns)):
                val = pivot.iloc[i, j]
                if pd.notna(val):
                    ax.text(j, i, f"{val:.1f}", ha="center", va="center", fontsize=8)

        ax.set_title("月度收益率热力图 (%)")
        fig.colorbar(im, ax=ax)
        plt.tight_layout()
        if save_path:
            Path(save_path).parent.mkdir(parents=True, exist_ok=True)
            plt.savefig(save_path, dpi=150)
        plt.close(fig)
