"""
trading_engine/agents/macro_agent.py

Agent 8: Macro Agent
- DXY trend (dollar strength — inverse to BTC/stocks)
- Risk-on / risk-off assessment
- ETF flow signals (crypto: BTC ETF flows; stocks: SPY/QQQ flow)
- LLM synthesizes macro context from structured data (NOT hallucinating prices)
"""
from __future__ import annotations
import json
import requests
from loguru import logger
from trading_engine.agents.base import AgentSignal, Signal
from trading_engine.data.market_data import MarketSnapshot
from trading_engine.config import settings
from trading_engine.utils.llm import call_llm


from trading_engine.utils.http import get_with_retry


import time

# Cache global macro data to prevent hitting rate limits across multiple assets in the same cycle
_MACRO_CACHE = {
    "dxy_trend": "unknown",
    "risk_mode": "unknown",
    "last_fetched": 0.0
}
CACHE_DURATION_SEC = 300  # Cache for 5 minutes

def _fetch_dxy_trend() -> str:
    """Approximate DXY trend via UUP ETF (USD bull ETF) from Massive."""
    try:
        # Try Yahoo Finance first (free, fast, no rate limits)
        try:
            import yfinance as yf
            ticker_obj = yf.Ticker("UUP")
            df = ticker_obj.history(period="1mo", interval="1d")
            if df is not None and len(df) >= 10:
                recent_avg = df["Close"].tail(5).mean()
                older_avg = df["Close"].tail(10).head(5).mean()
                if recent_avg > older_avg * 1.005:
                    return "rising"
                elif recent_avg < older_avg * 0.995:
                    return "falling"
                return "neutral"
        except Exception as yf_err:
            logger.warning(f"Yahoo Finance DXY fetch failed: {yf_err}. Trying Massive.")

        api_key = settings.get_massive_api_key
        if api_key:
            url = f"{settings.massive_api_base}/v2/aggs/ticker/UUP/range/1/day/2024-01-01/2099-01-01"
            params = {"adjusted": "true", "sort": "desc", "limit": 20, "apiKey": api_key}
            resp = get_with_retry(url, params=params, timeout=5)
            results = resp.json().get("results", [])
            if len(results) >= 10:
                recent_avg = sum(r["c"] for r in results[:5]) / 5
                older_avg = sum(r["c"] for r in results[5:10]) / 5
                if recent_avg > older_avg * 1.005:
                    return "rising"
                elif recent_avg < older_avg * 0.995:
                    return "falling"
                return "neutral"
    except Exception as e:
        logger.warning(f"DXY fetch error: {e}")
    return "unknown"


def _fetch_risk_mode() -> str:
    """Risk-on/off via VIX proxy: if VIX > 25 = risk-off, < 18 = risk-on."""
    try:
        # Try Yahoo Finance first (free, fast, no rate limits)
        try:
            import yfinance as yf
            ticker_obj = yf.Ticker("VIXY")
            df = ticker_obj.history(period="1mo", interval="1d")
            if df is not None and not df.empty:
                vixy_price = float(df["Close"].iloc[-1])
                if vixy_price > 25:
                    return "risk-off"
                elif vixy_price < 18:
                    return "risk-on"
                return "neutral"
        except Exception as yf_err:
            logger.warning(f"Yahoo Finance VIXY fetch failed: {yf_err}. Trying Massive.")

        api_key = settings.get_massive_api_key
        if api_key:
            url = f"{settings.massive_api_base}/v2/aggs/ticker/VIXY/range/1/day/2024-01-01/2099-01-01"
            params = {"adjusted": "true", "sort": "desc", "limit": 3, "apiKey": api_key}
            resp = get_with_retry(url, params=params, timeout=5)
            results = resp.json().get("results", [])
            if results:
                vixy_price = results[0]["c"]
                if vixy_price > 25:
                    return "risk-off"
                elif vixy_price < 18:
                    return "risk-on"
                return "neutral"
    except Exception as e:
        logger.warning(f"Risk mode fetch error: {e}")
    return "unknown"


def _update_macro_cache_if_expired():
    """Atomically updates the macro data cache if expired."""
    now = time.time()
    if not _MACRO_CACHE["last_fetched"] or (now - _MACRO_CACHE["last_fetched"] >= CACHE_DURATION_SEC):
        logger.info("Macro cache expired or empty. Fetching fresh indicators...")
        _MACRO_CACHE["dxy_trend"] = _fetch_dxy_trend()
        _MACRO_CACHE["risk_mode"] = _fetch_risk_mode()
        _MACRO_CACHE["last_fetched"] = now


def _get_dxy_trend() -> str:
    _update_macro_cache_if_expired()
    return _MACRO_CACHE["dxy_trend"]


def _get_risk_mode() -> str:
    _update_macro_cache_if_expired()
    return _MACRO_CACHE["risk_mode"]


def _rule_based_macro(
    dxy_trend: str,
    risk_mode: str,
    asset_type: str,
) -> tuple[Signal, float, str]:
    """
    Deterministic macro signal — same inputs always yield same output.
    Encodes the rules that were previously delegated to the LLM prompt.
    Eliminates API cost and output variance.
    """
    is_crypto = asset_type in ("crypto", "spot", "perpetual")
    is_equity = asset_type in ("stock", "etf", "cfd")

    # ── Risk-off hard deterrent (highest priority) ─────────────────────
    if risk_mode == "risk-off":
        if is_crypto:
            return Signal.SELL, 75.0, "Risk-off regime (VIX elevated) — avoid high-beta crypto"
        elif is_equity:
            return Signal.SELL, 65.0, "Risk-off regime — avoid equities"

    # ── DXY trend rules ────────────────────────────────────────────────
    if dxy_trend == "rising":
        if is_crypto:
            return Signal.SELL, 65.0, "Rising DXY (dollar strength) — headwind for crypto"
        elif is_equity:
            return Signal.SELL, 55.0, "Rising DXY — moderate headwind for growth equities"
        return Signal.HOLD, 50.0, "Rising DXY — neutral for this asset class"

    if dxy_trend == "falling":
        if is_crypto:
            return Signal.BUY, 65.0, "Falling DXY (dollar weakness) — tailwind for crypto"
        elif is_equity:
            return Signal.BUY, 55.0, "Falling DXY — moderate tailwind for growth equities"
        return Signal.HOLD, 50.0, "Falling DXY — neutral for this asset class"

    # ── Risk-on positive confirmation ─────────────────────────────────
    if risk_mode == "risk-on":
        if is_crypto:
            return Signal.BUY, 60.0, "Risk-on regime — favourable for crypto"
        elif is_equity:
            return Signal.BUY, 55.0, "Risk-on regime — favourable for equities"

    # ── Neutral / unknown ─────────────────────────────────────────────
    return Signal.HOLD, 50.0, f"Neutral macro: DXY={dxy_trend}, risk_mode={risk_mode}"


def analyze(snap: MarketSnapshot) -> AgentSignal:
    reasons = []

    dxy_trend = _get_dxy_trend()
    risk_mode = _get_risk_mode()

    # Store on snapshot for reference by other agents / dashboard
    snap.dxy_trend = dxy_trend
    snap.risk_mode = risk_mode

    signal, confidence, reason = _rule_based_macro(dxy_trend, risk_mode, snap.asset_type)
    reasons.append(f"DXY: {dxy_trend} | Risk mode: {risk_mode} | {reason}")

    return AgentSignal(
        agent="macro",
        signal=signal,
        confidence=confidence,
        reason=" | ".join(reasons),
        raw_data={"dxy_trend": dxy_trend, "risk_mode": risk_mode},
    )

