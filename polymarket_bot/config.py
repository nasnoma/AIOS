"""
polymarket_bot/config.py

Configuration settings loaded from environment variables for the Polymarket Scalping Bot.
"""
from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[ROOT_DIR / ".env", Path(__file__).parent / ".env"],
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Polymarket / Polygon Wallet ────────────────────────────────
    polymarket_private_key: str = ""
    polymarket_funder: str = ""
    polymarket_signature_type: int = 0         # 0=EOA, 1=POLY_PROXY (Magic), 2=POLY_GNOSIS_SAFE, 3=POLY_1271 (Privy/New)

    # ── LLM (OpenRouter) ───────────────────────────────────────────
    openrouter_api_key: str = ""
    llm_model: str = "google/gemini-flash-1.5"   # Fast, cheap model for advisory tasks
    llm_advisor_interval_cycles: int = 20      # Run LLM every N completed windows
    llm_auto_tune: bool = False

    # ── Alerts (Telegram) ──────────────────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Trading Mode ───────────────────────────────────────────────
    trading_mode: str = "paper"                  # paper | live
    account_size: float = 1000.0                 # Virtual USD for paper mode

    # ── Target Markets ─────────────────────────────────────────────
    target_assets: str = "BTC,ETH"               # Comma-separated: BTC | ETH | SOL

    # ── Risk Controls ──────────────────────────────────────────────
    max_risk_per_trade_pct: float = 0.01         # 1% of bankroll per trade max
    max_concurrent_positions: int = 3          # Max open positions at one time
    max_daily_loss_usd: float = 100.0            # Circuit breaker: halt after $100 loss/day
    max_acceptable_slippage: float = 0.01
    min_pool_liquidity_usd: float = 2000.0
    take_profit_pct: float = 0.12                # Take profit early if we reach 12% gain

    # ── Strategy Parameters ────────────────────────────────────────
    spread_arb_threshold: float = 0.98           # Buy both legs if YES+NO sum < this
    momentum_threshold_usd: float = 15.0         # Min BTC $ move in 30s to trigger signal
    min_confidence: float = 0.70                 # Min implied prob on favoured side (0–1)
    min_signal_confidence: float = 0.35          # Min combined confidence
    max_entry_price: float = 0.95                # Don't buy shares above 95¢
    entry_window_min_s: int = 45               # Don't enter before 45s into a 5-min window
    entry_window_max_s: int = 150              # Don't enter after 150s into a 5-min window
    momentum_lookback_s: int = 30              # BTC price lookback window for momentum calc

    # ── Bybit settings (required for price feed) ──────────────────
    bybit_testnet: bool = True

    # ── Properties ─────────────────────────────────────────────────
    @property
    def assets(self) -> list[str]:
        return [a.strip() for a in self.target_assets.split(",") if a.strip()]

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"


settings = Settings()
