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

    # ── CEX-DEX Arbitrage Parameters ──────────
    trading_mode: str = "paper"             # "paper" | "live"
    account_size: float = 500.0             # Split equally (e.g. $250 on Bybit, $250 on Raydium)
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    jupiter_api_key: str = ""               # Optional Jupiter API key to bypass rate limits
    solana_wallet_private_key: str = ""
    min_arbitrage_spread_pct: float = 0.0002 # Net profit threshold to execute (0.02%)
    trade_size_usdt: float = 100.0          # Swap size per leg ($100 USDT)
    max_price_age_s: float = 10.0           # Max age of price quotes in seconds
    dedicated_rpc_url: str = ""             # If configured, reduces simulated latency
    max_paper_drawdown_pct: float = 0.05    # Max drawdown percentage to pause paper trading
    jupiter_429_sim_prob: float = 0.0       # Probability of simulated 429 rate limit errors (disabled)
    live_tx_simulation_only: bool = True    # If True, live mode simulates transactions instead of broadcasting them

    # ── Dashboard ──────────────────────────────
    api_port: int = 8080


settings = Settings()
