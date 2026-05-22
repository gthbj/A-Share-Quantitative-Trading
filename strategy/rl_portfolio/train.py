"""RL 模型训练脚本。

用法：
    python strategy/rl_portfolio/train.py \
        --returns-csv data/returns.csv \
        --timesteps 10000 \
        --save-path strategy/rl_portfolio/models/ppo_v1.zip
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from strategy.rl_portfolio.env import SimpleTradingEnv

# stable-baselines3 为可选依赖
try:
    from stable_baselines3 import PPO

    HAS_SB3 = True
except ImportError:
    HAS_SB3 = False


def train(
    returns: np.ndarray,
    timesteps: int = 10000,
    save_path: str = "strategy/rl_portfolio/models/ppo_v1.zip",
) -> None:
    if not HAS_SB3:
        raise ImportError("需要 stable-baselines3，请执行 `pip install stable-baselines3`")

    env = SimpleTradingEnv(returns)
    model = PPO(
        "MlpPolicy",
        env,
        learning_rate=0.0003,
        n_steps=2048,
        batch_size=64,
        n_epochs=10,
        gamma=0.99,
        gae_lambda=0.95,
        verbose=1,
    )
    model.learn(total_timesteps=timesteps)

    Path(save_path).parent.mkdir(parents=True, exist_ok=True)
    model.save(save_path)
    print(f"模型已保存至: {save_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--returns-csv", required=True)
    parser.add_argument("--timesteps", type=int, default=10000)
    parser.add_argument("--save-path", default="strategy/rl_portfolio/models/ppo_v1.zip")
    args = parser.parse_args()

    df = pd.read_csv(args.returns_csv)
    returns = df["return"].values
    train(returns, timesteps=args.timesteps, save_path=args.save_path)


if __name__ == "__main__":
    main()
