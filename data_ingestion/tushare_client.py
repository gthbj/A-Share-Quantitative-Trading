from __future__ import annotations

import os


REQUIREMENTS_INSTALL_HINT = "Install dependencies with: python -m pip install -r requirements.txt"
DEFAULT_HTTP_URL = "http://118.89.66.41:8010/"


def require_tushare():
    try:
        import tushare as ts
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency: tushare. {REQUIREMENTS_INSTALL_HINT}") from exc
    return ts


def env_value(name: str) -> str:
    return os.environ.get(name, "").strip()


def tushare_token(config: dict) -> str:
    token_env = config.get("tushare", {}).get("token_env", "TUSHARE_TOKEN")
    token = env_value(token_env)
    if not token:
        raise RuntimeError(f"Tushare token is missing. Set environment variable {token_env}.")
    return token


def tushare_http_url(config: dict) -> str | None:
    tushare_config = config.get("tushare", {})
    http_url_env = tushare_config.get("http_url_env", "TUSHARE_HTTP_URL")
    http_url = env_value(http_url_env) or str(tushare_config.get("http_url") or "").strip()
    if not http_url:
        return None
    return http_url


def create_tushare_pro(config: dict):
    ts = require_tushare()
    tushare_config = config.get("tushare", {})
    token = tushare_token(config)
    ts.set_token(token)
    pro = ts.pro_api(token)
    http_url = tushare_http_url(config)
    if http_url:
        pro._DataApi__http_url = http_url
    if tushare_config.get("request_timeout_seconds") is not None:
        pro._DataApi__timeout = int(tushare_config["request_timeout_seconds"])
    return pro
