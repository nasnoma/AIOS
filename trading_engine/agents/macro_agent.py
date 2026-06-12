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
        api_key = settings.get_massive_api_key
        if not api_key:
            return "unknown"
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
        api_key = settings.get_massive_api_key
        if not api_key:
            return "unknown"
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


def _llm_macro(dxy_trend: str, risk_mode: str, symbol: str, asset_type: str) -> tuple[Signal, float, str]:
    """LLM synthesizes structured macro data."""
    prompt = f"""You are MacroAgent analyzing {symbol} ({asset_type}).

Macro data:
- DXY (dollar) trend: {dxy_trend}
- Market risk mode: {risk_mode}
- Asset type: {asset_type}

Rules:
- Rising DXY = bearish for crypto and growth stocks
- Falling DXY = bullish for crypto and growth stocks
- Risk-off = avoid crypto and high-beta stocks
- Risk-on = favorable for crypto and growth stocks

Based on this macro context, output JSON:
{{"signal": "BUY|SELL|HOLD", "confidence": 0-100, "reason": "one sentence"}}

Output ONLY the JSON object."""

    try:
        content = call_llm(prompt)

        if "```" in content:
            content = content.split("```")[1].strip().lstrip("json").strip()
        data = json.loads(content)
        return Signal(data["signal"].upper()), float(data.get("confidence", 50)), data.get("reason", "")

    except Exception as e:
        logger.warning(f"LLM macro failed: {e}")
        return Signal.HOLD, 45.0, "Macro analysis unavailable"


def analyze(snap: MarketSnapshot) -> AgentSignal:
    reasons = []

    dxy_trend = _get_dxy_trend()
    risk_mode = _get_risk_mode()

    # Store on snapshot for reference
    snap.dxy_trend = dxy_trend
    snap.risk_mode = risk_mode

    signal, confidence, reason = _llm_macro(dxy_trend, risk_mode, snap.symbol, snap.asset_type)
    reasons.append(f"DXY: {dxy_trend} | Risk mode: {risk_mode} | {reason}")

    return AgentSignal(
        agent="macro",
        signal=signal,
        confidence=confidence,
        reason=" | ".join(reasons),
        raw_data={"dxy_trend": dxy_trend, "risk_mode": risk_mode},
    )
