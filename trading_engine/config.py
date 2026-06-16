"""
trading_engine/config.py
Central configuration — loads from .env
"""
from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from pathlib import Path

ROOT_DIR = Path(__file__).parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── LLM ─────────────────────────────────
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o"

    # ── Market Data ─────────────────────────
    polygon_api_key: str = ""
    massive_api_key: str = ""
    massive_api_base: str = "https://api.massive.com"
    binance_api_key: str = ""
    binance_api_secret: str = ""
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_demo_trading: bool = True

    # ── Broker ──────────────────────────────
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    crypto_exchange: str = "binance"
    crypto_testnet: bool = False

    # ── Alerts ──────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Database ────────────────────────────
    database_url: str = "postgresql://trader:secret@localhost:5432/trading_engine"

    # ── Trading ─────────────────────────────
    trading_mode: str = "paper"          # paper | live | signal_only
    default_assets: str = "BTC/USDT,ETH/USDT"
    default_stock_assets: str = "AAPL/USDT:USDT,TSLA/USDT:USDT,NVDA/USDT:USDT"
    default_watchlist: str = "BTC/USDT,ETH/USDT,SOL/USDT,AAPL/USDT:USDT,TSLA/USDT:USDT,NVDA/USDT:USDT,MSFT/USDT:USDT,AMZN/USDT:USDT"
    timeframe: str = "4h"
    signal_interval_minutes: int = 240

    # ── Risk ────────────────────────────────
    account_size: float = 10_000.0
    max_risk_per_trade: float = 0.02
    max_portfolio_heat: float = 0.06
    kelly_fraction: float = 0.25
    min_agent_agreement: int = 3
    min_avg_confidence: float = 30.0
    atr_multiplier: float = 2.2
    rr_ratio: float = 2.5
    stop_loss_pct_max: float = 0.10
    max_concurrent_positions: int = 20

    # ── Self Healing ────────────────────────
    self_healing_consecutive_losses: int = 2
    self_healing_cooldown_hours: float = 24.0
    self_healing_iterations: int = 15

    # ── Bounty Hunter ───────────────────────
    bounty_hunter_enabled: bool = True
    bounty_hunter_interval_hours: int = 1
    bounty_hunter_interval_minutes: int = 0
    bounty_hunter_watchlist: str = ""

    # ── Bybit CFD Trading ────────────────────
    # US stock CFD linear perpetuals on Bybit (TICKER/USDT:USDT format)
    bybit_cfd_stocks: str = "AAPL/USDT:USDT,TSLA/USDT:USDT,NVDA/USDT:USDT,MSFT/USDT:USDT,AMZN/USDT:USDT,GOOGL/USDT:USDT"
    # Precious metals linear perpetuals on Bybit
    bybit_cfd_metals: str = "XAU/USDT:USDT,XAG/USDT:USDT"
    # Master CFD switch
    cfd_enabled: bool = True
    # If True: scan pre-market (13:00 UTC EDT) and after-hours (21:00 UTC EDT)
    extended_cfd_hours: bool = True

    # ── API ─────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    @property
    def crypto_assets(self) -> list[str]:
        return [a.strip() for a in self.default_assets.split(",")]

    @property
    def stock_assets(self) -> list[str]:
        return [a.strip() for a in self.default_stock_assets.split(",")]

    @property
    def watchlist_assets(self) -> list[str]:
        return [a.strip() for a in self.default_watchlist.split(",")]

    @property
    def bounty_hunter_watchlist_assets(self) -> list[str] | None:
        if not self.bounty_hunter_watchlist:
            return None
        return [a.strip() for a in self.bounty_hunter_watchlist.split(",") if a.strip()]

    @property
    def cfd_stock_assets(self) -> list[str]:
        return [a.strip() for a in self.bybit_cfd_stocks.split(",") if a.strip()]

    @property
    def cfd_metal_assets(self) -> list[str]:
        return [a.strip() for a in self.bybit_cfd_metals.split(",") if a.strip()]

    @property
    def all_cfd_assets(self) -> list[str]:
        return self.cfd_stock_assets + self.cfd_metal_assets

    @property
    def get_massive_api_key(self) -> str:
        return self.massive_api_key or self.polygon_api_key


settings = Settings()

import os
if "PORT" in os.environ:
    settings.api_port = int(os.environ["PORT"])
