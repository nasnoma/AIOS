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

    # ── Grid Market Maker Parameters ───────────
    trading_mode: str = "paper"             # "paper" | "live"
    account_size: float = 500.0             # Default base capital if live fetch fails
    grid_levels: int = 5                    # Number of buy/sell levels (10 orders total)
    grid_span_pct: float = 0.015            # Total percentage width of the grid (1.5%)
    order_size_usdt: float = 10.0           # USDT size per order level
    inventory_target_pct: float = 0.50      # Target ratio of asset value (50/50)
    inventory_shading_factor: float = 0.15  # Shifts prices to correct inventory imbalances
    drift_trigger_pct: float = 0.003        # Cancel & replace grid if mid-price drifts by >0.3%
    max_price_age_s: float = 2.0            # Discard price updates older than this limit

    # ── Dashboard ──────────────────────────────
    api_port: int = 8080


settings = Settings()
