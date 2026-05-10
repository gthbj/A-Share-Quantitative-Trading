"""回测结果可视化：收益曲线、回撤、持仓分布等。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import matplotlib
import matplotlib.pyplot as plt
import pandas as pd

# 非交互环境使用 Agg 后端
matplotlib.use("Agg")


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
        monthly = returns.resample("ME").apply(lambda x: (1 + x).prod() - 1) * 100
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
