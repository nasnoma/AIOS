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
    default_watchlist: str = (
        "BTC/USDT,ETH/USDT,SOL/USDT,AAPL/USDT:USDT,TSLA/USDT:USDT,NVDA/USDT:USDT,MSFT/USDT:USDT,AMZN/USDT:USDT,"
        "TRANSEXPR/NGX,WEMABANK/NGX,VFDGROUP/NGX,NEIMETH/NGX,NPFMCRFBK/NGX,VERITASKAP/NGX,AIICO/NGX,UNIVINSURE/NGX,ZICHIS/NGX,"
        "NVDA/BAMBOO,META/BAMBOO,TSLA/BAMBOO,JPM/BAMBOO,NFLX/BAMBOO"
    )
    timeframe: str = "4h"
    signal_interval_minutes: int = 240

    # ── Risk ────────────────────────────────
    account_size: float = 10_000.0
    max_risk_per_trade: float = 0.02
    max_portfolio_heat: float = 0.06
    kelly_fraction: float = 0.25
    min_agent_agreement: int = 3
    min_avg_confidence: float = 30.0
    atr_multiplier: float = 2.8
    rr_ratio: float = 3.0
    stop_loss_pct_max: float = 0.10
    max_concurrent_positions: int = 12

    # ── Self Healing ────────────────────────────────────────
    self_healing_consecutive_losses: int = 2
    self_healing_cooldown_hours: float = 24.0
    self_healing_iterations: int = 15

    # ── Circuit Breaker ─────────────────────────────────────
    # Halt ALL new entries once daily realized losses exceed this amount (USD).
    # Set to 0 to disable.
    daily_loss_limit_usd: float = 300.0

    # ── Market Regime Filter ─────────────────────────────────
    # Only allow LONG crypto entries when BTC is above its N-period MA on the
    # configured timeframe.  Set regime_filter_enabled=False to bypass.
    regime_filter_enabled: bool = True
    regime_btc_ma_period: int = 50
    regime_filter_equities_enabled: bool = True
    regime_us_index: str = "SPY"
    regime_ngx_proxy: str = "DANGCEM/NGX"
    regime_equities_ma_period: int = 200
    crypto_use_perpetuals: bool = False
    short_position_multiplier: float = 0.75

    # ── Session Awareness & Volatility Filters ──────
    crypto_peak_sessions_only: bool = False
    crypto_peak_sessions_reduce_size: bool = True
    min_atr_pct: float = 0.15
    min_bb_width: float = 0.015

    # ── Confidence-Weighted Position Sizing ──────────
    # Scales position size proportionally to judge confidence score.
    # confidence=neutral (75) → 1.0× baseline size
    # confidence=min (48) → position_size_min_weight (0.6×) — undersized, cautious
    # confidence=max (100) → position_size_max_weight (1.4×) — oversized, high conviction
    # Formula: weight = min_w + (max_w - min_w) * (conf - min_conf) / (max_conf - min_conf)
    # Clamped to [min_w, max_w].
    confidence_sizing_enabled: bool = True
    position_size_min_weight: float = 0.6    # at minimum confidence threshold
    position_size_max_weight: float = 1.4    # at maximum confidence (100)
    confidence_sizing_neutral: float = 75.0  # pivot: no adjustment at this confidence
    # NGX stocks with fewer than N live/backtest trades get an extra conservative discount.
    # Covers thin-data stocks like TRANSEXPR (7 trades), CHAMS (3), etc.
    low_trade_count_discount: float = 0.75   # multiply size by this for thin NGX stocks
    low_trade_count_threshold: int = 10      # trades below this → apply discount


    # ── Bounty Hunter ───────────────────────
    bounty_hunter_enabled: bool = True
    bounty_hunter_interval_hours: int = 1
    bounty_hunter_interval_minutes: int = 0
    bounty_hunter_watchlist: str = ""
    # Scan strategy: "momentum" (buy confirmed strength) | "oversold" (mean-reversion) | "volume" | "hot"
    # momentum has stronger positive edge on 4H crypto timeframes
    bounty_hunter_scan_mode: str = "momentum"

    # ── Bybit CFD Trading ────────────────────
    # US stock CFD linear perpetuals on Bybit (TICKER/USDT:USDT format)
    bybit_cfd_stocks: str = "AAPL/USDT:USDT,TSLA/USDT:USDT,NVDA/USDT:USDT,MSFT/USDT:USDT,AMZN/USDT:USDT,GOOGL/USDT:USDT"
    # Precious metals linear perpetuals on Bybit
    bybit_cfd_metals: str = "XAU/USDT:USDT,XAG/USDT:USDT"
    # Master CFD switch
    cfd_enabled: bool = True
    # If True: scan pre-market (13:00 UTC EDT) and after-hours (21:00 UTC EDT)
    extended_cfd_hours: bool = True

    # ── Bamboo API (Nigerian Stocks) ────────
    bamboo_api_key: str = ""
    bamboo_username: str = ""
    bamboo_password: str = ""
    bamboo_user_id: str = ""
    bamboo_cscs: str = ""
    bamboo_chn: str = ""
    bamboo_base_url: str = "https://powered-by-bamboo-sandbox.investbamboo.com"
    bamboo_subject_type: str = "tenant"
    bamboo_webhook_auth_hash: str = ""
    # 20-stock NGX universe — filtered by WFO (passing OOS quality filter)
    default_ngx_assets: str = (
        "CHAMS/NGX,NGXGROUP/NGX,GUINEAINS/NGX,GTCO/NGX,CONHALLPLC/NGX,"
        "INTENEGINS/NGX,MBENEFIT/NGX,DEAPCAP/NGX,UPDCREIT/NGX,WAPIC/NGX,JAPAULGOLD/NGX"
    )
    # Bamboo US assets — filtered by WFO (passing OOS quality filter)
    default_bamboo_us_assets: str = (
        "AAPL/BAMBOO,MSFT/BAMBOO,AMZN/BAMBOO,GOOGL/BAMBOO,AMD/BAMBOO"
    )

    # ── Historical NGX Data Providers ────────
    eodhd_api_key: str = ""
    ngx_pulse_api_key: str = ""

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

    @property
    def ngx_assets(self) -> list[str]:
        return [a.strip() for a in self.default_ngx_assets.split(",") if a.strip()]

    @property
    def bamboo_us_assets(self) -> list[str]:
        return [a.strip() for a in self.default_bamboo_us_assets.split(",") if a.strip()]


settings = Settings()

import os
if "PORT" in os.environ:
    settings.api_port = int(os.environ["PORT"])
