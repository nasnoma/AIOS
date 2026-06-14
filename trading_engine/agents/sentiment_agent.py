"""
trading_engine/agents/sentiment_agent.py

Agent 7: Sentiment Agent
- Uses Fear & Greed Index (quantitative) as primary signal
- Uses LLM to synthesize pre-fetched news headlines (NOT raw market data)
- LLM call is minimal: summarize 5 headlines → BUY/SELL/HOLD
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


def _get_news_headlines(symbol: str) -> list[str]:
    """Fetch latest news headlines for the symbol."""
    headlines = []
    api_key = settings.get_massive_api_key
    if api_key:
        ticker = symbol.replace("/USDT", "").replace("/USD", "")
        url = f"{settings.massive_api_base}/v2/reference/news"
        params = {"ticker": ticker, "limit": 5, "apiKey": api_key}
        try:
            resp = get_with_retry(url, params=params, timeout=5)
            if resp.ok:
                for item in resp.json().get("results", []):
                    headlines.append(item.get("title", ""))
        except Exception as e:
            logger.warning(f"News fetch error: {e}")
    return headlines[:5]


def _llm_sentiment(
    headlines: list[str],
    tweets: list[str] = None,
    stocktwits_raw: str = "",
    symbol: str = ""
) -> tuple[Signal, float, str]:
    """Use LLM to interpret headlines and social feeds → signal."""
    context_blocks = []
    
    if headlines:
        context_blocks.append("News headlines (last 24h):\n" + "\n".join(f"- {h}" for h in headlines))
        
    if tweets:
        context_blocks.append("Scraped Tweets / X Posts:\n" + "\n".join(f"- {t}" for t in tweets[:8]))
        
    if stocktwits_raw:
        context_blocks.append(f"Scraped Stocktwits Feed:\n{stocktwits_raw[:1000]}")
        
    if not context_blocks:
        return Signal.HOLD, 50.0, "No sentiment source data available"
        
    combined_context = "\n\n".join(context_blocks)

    from pathlib import Path
    prompt_path = Path(__file__).parent.parent / "prompts" / "sentiment_prompt.md"
    if prompt_path.exists():
        prompt = prompt_path.read_text().format(symbol=symbol, combined_context=combined_context)
    else:
        prompt = f"""You are SentimentAgent analyzing {symbol}.

Scraped Social Media and News Context:
{combined_context}

Based ONLY on this news and social sentiment context, output JSON:
{{"signal": "BUY|SELL|HOLD", "confidence": 0-100, "reason": "one sentence"}}

Rules:
- BUY = clearly positive news or extremely bullish social/retail sentiment/hype.
- SELL = clearly negative news, crash fears, hacks, or extremely bearish panic.
- HOLD = mixed, neutral, or conflicting sentiment.
- confidence reflects how strong/consistent the sentiment is.
Output ONLY the JSON object."""

    try:
        content = call_llm(prompt)

        # Parse JSON response
        if "```" in content:
            content = content.split("```")[1].strip().lstrip("json").strip()
        data = json.loads(content)
        signal = Signal(data["signal"].upper())
        confidence = float(data.get("confidence", 50))
        reason = data.get("reason", "")
        return signal, confidence, reason

    except Exception as e:
        logger.warning(f"LLM sentiment failed: {e}")
        return Signal.HOLD, 40.0, f"LLM error: {str(e)[:50]}"


def analyze(snap: MarketSnapshot) -> AgentSignal:
    score = 0
    max_score = 4
    reasons = []

    # ── Fear & Greed Index ─────────────────────────────
    fg = snap.fear_greed_index
    fg_label = snap.fear_greed_label or "Unknown"
    if fg is not None:
        if fg <= 25:          # Extreme Fear → contrarian BUY
            score += 2
            reasons.append(f"Extreme Fear ({fg}, {fg_label}) — contrarian bullish signal")
        elif fg <= 45:        # Fear → mild buy
            score += 1
            reasons.append(f"Fear ({fg}, {fg_label}) — cautiously bullish")
        elif fg >= 80:        # Extreme Greed → contrarian SELL
            score -= 2
            reasons.append(f"Extreme Greed ({fg}, {fg_label}) — contrarian bearish")
        elif fg >= 60:
            score -= 1
            reasons.append(f"Greed ({fg}, {fg_label}) — caution")
        else:
            reasons.append(f"Neutral sentiment ({fg}, {fg_label})")

    # ── LLM News & Social Sentiment ────────────────────
    headlines = snap.news_headlines if snap.news_headlines else _get_news_headlines(snap.symbol)
    llm_signal, llm_conf, llm_reason = _llm_sentiment(
        headlines=headlines,
        tweets=snap.tweets,
        stocktwits_raw=snap.stocktwits_raw,
        symbol=snap.symbol
    )
    reasons.append(f"News/Social: {llm_reason} (LLM conf={llm_conf:.0f}%)")

    if llm_signal == Signal.BUY and llm_conf > 65:
        score += 2
    elif llm_signal == Signal.SELL and llm_conf > 65:
        score -= 2
    elif llm_signal == Signal.BUY:
        score += 1
    elif llm_signal == Signal.SELL:
        score -= 1

    # ── Map → signal ──────────────────────────────────
    normalized = (score / max_score + 1) / 2
    confidence = round(max(0, min(100, normalized * 100)), 1)

    if score >= 2:
        signal = Signal.BUY
    elif score <= -2:
        signal = Signal.SELL
    else:
        signal = Signal.HOLD

    return AgentSignal(
        agent="sentiment",
        signal=signal,
        confidence=confidence,
        reason=" | ".join(reasons),
        raw_data={
            "fear_greed": fg,
            "llm_signal": str(llm_signal),
            "headlines": headlines,
            "tweets_count": len(snap.tweets)
        },
    )
