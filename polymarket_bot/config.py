"""
polymarket_bot/config.py

Configuration settings loaded from environment variables.
"""
from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Bybit API Credentials ──────────────────
    bybit_api_key: str = "vU8Cg21arhQjUEWxvr"
    bybit_api_secret: str = "EpbpMD2kpjxWOVeh94jwhHHNfREsFAAZSenk"
    bybit_testnet: bool = True

    # ── Telegram Alerts ────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Arbitrage Parameters ───────────────────
    trading_mode: str = "paper"             # "paper" | "live"
    account_size: float = 500.0             # Default base capital if live fetch fails
    min_net_edge_pct: float = 0.0015        # Minimum net profit above 0.30% fee (Maker/Taker spot fees)
    position_size_pct: float = 0.80         # Use 80% of USDT balance per trade to allow buffer

    # ── Dashboard ──────────────────────────────
    api_port: int = 8080


settings = Settings()
