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
    bybit_api_key: str = "NzSg7VszfTXPjy2VdS"
    bybit_api_secret: str = "K9hHCNRooqs5Jik2Ez4Huis9XpxjCSkOAFcL"
    bybit_demo_trading: bool = False


    # ── Broker ──────────────────────────────
    crypto_exchange: str = "binance"
    crypto_testnet: bool = False

    # ── Alerts ──────────────────────────────
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Database ────────────────────────────
    database_url: str = "postgresql://trader:secret@localhost:5432/trading_engine"

    # ── Trading ─────────────────────────────
    trading_mode: str = "paper"          # paper | live | signal_only
    default_assets: str = "BTC/USDT,ETH/USDT,SOL/USDT"
    default_watchlist: str = "BTC/USDT,ETH/USDT,SOL/USDT"
    timeframe: str = "4h"
    signal_interval_minutes: int = 240

    # ── Risk ────────────────────────────────
    account_size: float = 999.83

    max_risk_per_trade: float = 0.02
    max_portfolio_heat: float = 0.06
    kelly_fraction: float = 0.25
    min_agent_agreement: int = 5
    min_avg_confidence: float = 58.0
    atr_multiplier: float = 2.8
    rr_ratio: float = 3.0
    stop_loss_pct_max: float = 0.10
    max_concurrent_positions: int = 6
    max_trades_per_day: int = 8
    two_strike_window_hours: float = 2.0
    two_strike_count: int = 2
    crypto_use_5m_atr: bool = False
    max_hold_time_minutes: int = 720
    min_velocity_usd_30s: float = 0.0
    simple_ensemble_enabled: bool = False
    scalping_mode: bool = True


    # ── Self Healing ────────────────────────────────────────
    self_healing_consecutive_losses: int = Field(default=4, ge=2, le=4)
    self_healing_cooldown_hours: float = 24.0
    self_healing_iterations: int = 15

    # ── Circuit Breaker ─────────────────────────────────────
    # Halt ALL new entries once daily realized losses exceed this amount (USD).
    # Set to 0 to disable.
    daily_loss_limit_usd: float = 150.0
    max_position_usd: float = 3000.0    # hard cap per trade regardless of account size

    # ── Rolling Shut-off ─────────────────────────────────────
    rolling_shutoff_enabled: bool = True
    rolling_shutoff_window: int = 30
    rolling_shutoff_min_winrate: float = 0.38
    rolling_shutoff_min_sharpe: float = 0.25
    rolling_shutoff_max_drawdown: float = 0.08
    rolling_shutoff_since: str = "2026-07-16T00:00:00+00:00"  # Filter out old trades before the major code upgrade

    # ── Market Regime Filter ─────────────────────────────────
    # Only allow LONG crypto entries when BTC is above its N-period MA on the
    # configured timeframe.  Set regime_filter_enabled=False to bypass.
    regime_filter_enabled: bool = True
    regime_btc_ma_period: int = 50
    regime_btc_timeframe: str = "1d"
    crypto_use_perpetuals: bool = True
    short_position_multiplier: float = 0.75

    # ── Session Awareness & Volatility Filters ──────
    crypto_peak_sessions_only: bool = False
    crypto_peak_sessions_reduce_size: bool = True
    min_atr_pct: float = 0.15
    min_bb_width: float = 0.015

    # ── Per-Symbol High-Caution Controls ─────────────
    # Symbols listed here require higher agent consensus and have a lower position cap.
    # Use this for alts with erratic structure / low liquidity.
    high_caution_symbols: str = "NEAR/USDT,NEAR/USDT:USDT,ETH/USDT,ETH/USDT:USDT,SOL/USDT,SOL/USDT:USDT,AVAX/USDT,AVAX/USDT:USDT,BNB/USDT,BNB/USDT:USDT,XRP/USDT,XRP/USDT:USDT"
    high_caution_min_agreement: int = 7   # out of 8 agents
    high_caution_max_position_usd: float = 1500.0

    # ── Confidence-Weighted Position Sizing ──────────
    # Scales position size proportionally to judge confidence score.
    # confidence=neutral (75) → 1.0× baseline size
    # confidence=min (48) → position_size_min_weight (0.6×) — undersized, cautious
    # confidence=max (100) → position_size_max_weight (1.4×) — oversized, high conviction
    # Formula: weight = min_w + (max_w - min_w) * (conf - min_conf) / (max_c - min_c)
    # Clamped to [min_w, max_w].
    confidence_sizing_enabled: bool = True
    position_size_min_weight: float = 0.6    # at minimum confidence threshold
    position_size_max_weight: float = 1.4    # at maximum confidence (100)
    confidence_sizing_neutral: float = 75.0  # pivot: no adjustment at this confidence

    # ── Regime-Adaptive Routing ───────────────────────
    # NOTE: 365d walk-forward backtest on BTC/ETH/SOL (4h + 1h) showed mean-
    # reversion mode REDUCES edge (PF 0.99 → 0.69-0.87, WR 47% → 38-42%).
    # Disabled by default: set mean_reversion_adx_threshold > 100 to never
    # trigger. Re-enable only if a forward test on a NEW market/timeframe
    # demonstrates PF > 1.3 out-of-sample.
    mean_reversion_adx_threshold: float = 999.0   # disabled — was 25.0
    mean_reversion_atr_multiplier: float = 1.5
    mean_reversion_rr_ratio: float = 1.5
    mean_reversion_rsi_oversold: float = 35.0
    mean_reversion_rsi_overbought: float = 65.0

    # ── Let Winners Run ───────────────────────────────
    # NOTE: backtest showed TP cancellation hurts at bar resolution (intra-
    # bar ordering ambiguity: TP would hit before cancellation in live).
    # Disabled by default. Trailing SL ratchet in the execution layer
    # still protects profits without cancelling the fixed TP.
    let_winners_run_enabled: bool = False
    runner_activation_atr: float = 1.5
    runner_max_profit_pct: float = 1.50    # hard safety cap (150% gain) even when running

    # ── Bounty Hunter ───────────────────────
    bounty_hunter_enabled: bool = True
    bounty_hunter_interval_hours: int = 1
    bounty_hunter_interval_minutes: int = 0
    bounty_hunter_watchlist: str = "BTC/USDT"  # BTC-only until altcoin win rate improves (audit: ETH 0%, SOL 20%, NEAR 0%, AVAX 0%)
    # Scan strategy: "momentum" (buy confirmed strength) | "oversold" (mean-reversion) | "volume" | "hot"
    # momentum has stronger positive edge on 4H crypto timeframes
    bounty_hunter_scan_mode: str = "momentum"

    # ── Delta-Neutral Funding Carry ───────────────────
    # Harvests perpetual funding rates via delta-neutral spot+perp legs.
    # This is a NON-DIRECTIONAL strategy: it profits from funding, not price.
    # Runs in parallel with the directional engine.
    carry_enabled: bool = False               # disabled by default — enable after reading docs
    carry_min_apy: float = 15.0                # min annualized carry % to open position
    carry_borrow_cost_apy: float = 5.0        # estimated spot borrow + opportunity cost (APY %)
    carry_margin_buffer_pct: float = 0.30     # keep 30% margin buffer on perp leg
    carry_max_positions: int = 3              # max concurrent carry positions
    carry_max_inversions: int = 2             # close after N consecutive adverse funding cycles
    carry_max_hold_cycles: int = 90           # max funding cycles (~30 days at 3/day)
    carry_max_basis_loss_pct: float = 0.05    # close if basis loss exceeds 5% of position size
    carry_scan_interval_hours: int = 8        # how often to scan for new carry opportunities
    carry_watchlist: str = "BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT,XRP/USDT,AVAX/USDT,LINK/USDT,ADA/USDT"
    carry_position_size_pct: float = 0.15     # % of account per carry position

    # ── DEPRECATED / LEGACY (COMPATIBILITY ONLY) ──────────────────
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    default_stock_assets: str = "AAPL/USDT:USDT,TSLA/USDT:USDT,NVDA/USDT:USDT"
    is_backtesting: bool = False
    regime_filter_equities_enabled: bool = True
    regime_us_index: str = "SPY"
    regime_ngx_proxy: str = "DANGCEM/NGX"
    regime_equities_ma_period: int = 200
    low_trade_count_discount: float = 0.75
    low_trade_count_threshold: int = 10
    bybit_cfd_stocks: str = "AAPL/USDT:USDT,TSLA/USDT:USDT,NVDA/USDT:USDT,MSFT/USDT:USDT,AMZN/USDT:USDT,GOOGL/USDT:USDT"
    bybit_cfd_metals: str = "XAU/USDT:USDT,XAG/USDT:USDT"
    cfd_enabled: bool = True
    extended_cfd_hours: bool = True
    bamboo_api_key: str = ""
    bamboo_username: str = ""
    bamboo_password: str = ""
    bamboo_user_id: str = ""
    bamboo_cscs: str = ""
    bamboo_chn: str = ""
    bamboo_base_url: str = "https://powered-by-bamboo-sandbox.investbamboo.com"
    bamboo_subject_type: str = "tenant"
    bamboo_webhook_auth_hash: str = ""
    default_ngx_assets: str = "CHAMS/NGX,NGXGROUP/NGX,GUINEAINS/NGX,GTCO/NGX,CONHALLPLC/NGX,INTENEGINS/NGX,MBENEFIT/NGX,DEAPCAP/NGX,UPDCREIT/NGX,WAPIC/NGX,JAPAULGOLD/NGX"
    default_bamboo_us_assets: str = "AAPL/BAMBOO,MSFT/BAMBOO,AMZN/BAMBOO,GOOGL/BAMBOO,AMD/BAMBOO"
    eodhd_api_key: str = ""
    ngx_pulse_api_key: str = ""

    # ── API ─────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    @property
    def crypto_assets(self) -> list[str]:
        return [a.strip() for a in self.default_assets.split(",") if a.strip()]

    @property
    def watchlist_assets(self) -> list[str]:
        return [a.strip() for a in self.default_watchlist.split(",") if a.strip()]

    @property
    def get_massive_api_key(self) -> str:
        return self.massive_api_key or self.polygon_api_key


settings = Settings()

import os
if "PORT" in os.environ:
    settings.api_port = int(os.environ["PORT"])


# ── Spot Grid Settings ────────────────────────────────────────────────────────
# All parameters for the regime-adaptive spot grid / DCA strategy.
# These live here so they can be overridden via Railway environment variables.

class SpotGridSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_prefix="SPOT_",          # env var: SPOT_ENABLED=true etc.
    )

    # ── Master Switch ──────────────────────────────────────────
    enabled: bool = True
    paper_mode: bool = False          # False = Live Real-Money Trading with Bybit Spot Limit Orders

    # Screened High-Velocity Halal Spot Assets on Bybit
    assets: str = "FET/USDT,NEAR/USDT,SUI/USDT,TIA/USDT,OP/USDT,APT/USDT,AVAX/USDT,SEI/USDT,SOL/USDT"


    # ── Capital ────────────────────────────────────────────────
    total_capital_pct: float = 0.90   # 90% active deployment; keep ~10% dry powder
    usdt_hard_reserve_pct: float = 0.10  # 10% USDT floor — pause buys before free cash is trapped in bags

    # ── Asset Split (Performance-Weighted by 30-Day Volatility & Cycle Potential) ──────
    btc_allocation_pct: float = 0.00   # 0% → BTC (low volatility, reallocated to top alts)
    eth_allocation_pct: float = 0.00   # 0% → ETH (low volatility, reallocated to top alts)
    sol_allocation_pct: float = 0.00   # 0% → SOL
    xaut_allocation_pct: float = 0.00  # 0% → XAUT

    # ── Fees ───────────────────────────────────────────────────
    fee_rate: float = 0.001           # 0.1% per side (Bybit spot maker/taker)

    # ── Regime Detection ───────────────────────────────────────
    regime_timeframe: str = "1h"
    regime_sma_fast: int = 50
    regime_sma_slow: int = 200
    regime_adx_period: int = 14
    regime_confirm_bars: int = 3      # consecutive bars to confirm regime flip

    # ── Grid Params: BULL ──────────────────────────────────────
    bull_grid_spacing: float = 0.0080 # 0.80% spacing in clean Bull trend
    bull_buy_levels: int = 4
    bull_sell_levels: int = 6
    bull_capital_deployed: float = 0.85
    bull_base_hold_pct: float = 0.30

    # ── Grid Params: RANGE (default) ───────────────────────────
    range_grid_spacing: float = 0.0120 # 1.20% fee-proof spacing (real cash profit per cycle)
    range_buy_levels: int = 5          # 5 clean dip levels ($75 - $120 per order)
    range_sell_levels: int = 5
    range_capital_deployed: float = 0.75
    range_base_hold_pct: float = 0.20

    # ── Grid Params: BEAR (engine is sell-only; buys forced off) ──
    bear_grid_spacing: float = 0.0250 # 2.50% wide spacing in downtrend (patient accumulation)
    bear_buy_levels: int = 0          # sell-only in BEAR
    bear_sell_levels: int = 3
    bear_capital_deployed: float = 0.0
    bear_base_hold_pct: float = 0.15


    # ── DCA (mean-reversion extra buys) ───────────────────────
    dca_bb_period: int = 20
    dca_bb_std: float = 2.5
    dca_rsi_period: int = 14
    dca_rsi_range: float = 36.0       # RSI threshold for RANGE regime
    dca_rsi_bull: float = 40.0        # RSI threshold for BULL
    dca_rsi_bear: float = 22.0        # RSI threshold for BEAR (extreme only)

    # ── Scheduler ─────────────────────────────────────────────
    grid_tick_minutes: int = 1        # 1-minute high-frequency tick interval for instant fill processing
    regime_check_hours: int = 1       # how often to re-evaluate regime
    sync_minutes: int = 5             # how often to sync balances with exchange

    @property
    def asset_list(self) -> list[str]:
        return [a.strip() for a in self.assets.split(",") if a.strip()]

    @property
    def asset_allocation(self) -> dict[str, float]:
        active_assets = self.asset_list
        # Volatility-weighted capital allocation weights:
        # High-ATR Group (1.30x): ICP, ARB, NEAR, RENDER, FET
        # Moderate-ATR Group (1.00x): UNI, ARKM, ADA, APT
        # Low-ATR Group (0.70x): SOL, AVAX, SUI
        weights = {
            "ICP/USDT":    1.30,
            "ARB/USDT":    1.30,
            "NEAR/USDT":   1.30,
            "RENDER/USDT": 1.30,
            "FET/USDT":    1.30,
            "UNI/USDT":    1.00,
            "ARKM/USDT":   1.00,
            "ADA/USDT":    1.00,
            "APT/USDT":    1.00,
            "SOL/USDT":    0.70,
            "AVAX/USDT":   0.70,
            "SUI/USDT":    0.70,
        }
        total_w = sum(weights.get(a, 1.0) for a in active_assets) or 1.0
        return {a: weights.get(a, 1.0) / total_w for a in active_assets}


spot_settings = SpotGridSettings()

