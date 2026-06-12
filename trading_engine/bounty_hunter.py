"""
trading_engine/bounty_hunter.py

Bounty Hunter Scanning Agent.
Scans hundreds of assets on Bybit (for Crypto) and Massive (for Stocks) in bulk,
filters them by liquidity/price-action, and runs deep multi-agent evaluations
on the top candidates.
"""
from __future__ import annotations
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional
from loguru import logger

import ccxt
import requests

from trading_engine.config import settings
from trading_engine.orchestrator import run as run_pipeline
from trading_engine.utils.http import get_with_retry
from trading_engine.data.market_data import MarketSnapshot



def get_latest_trading_date() -> str:
    """Finds the most recent date that has Grouped Daily Aggregates data by checking backward."""
    api_key = settings.get_massive_api_key
    if not api_key:
        raise ValueError("Massive API key is required to scan stocks.")
        
    current = datetime.now(timezone.utc)
    for i in range(7):
        date_str = (current - timedelta(days=i)).strftime("%Y-%m-%d")
        url = f"https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/{date_str}"
        try:
            resp = get_with_retry(url, params={"adjusted": "true", "apiKey": api_key}, timeout=5)
            if resp.ok:
                data = resp.json()
                if data.get("resultsCount", 0) > 0:
                    logger.info(f"Found stock aggregates data for date: {date_str}")
                    return date_str
        except Exception as e:
            logger.warning(f"Error checking stock date {date_str}: {e}")
            
    # Default fallback to yesterday
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    logger.warning(f"Could not verify stock aggregates date, falling back to yesterday: {yesterday}")
    return yesterday


def scan_bybit_crypto(mode: str = "oversold", limit: int = 5, watchlist: Optional[list[str]] = None) -> list[str]:
    """
    Scans all spot USDT tickers on Bybit.
    Filters by liquidity (> 1,000,000 USDT) and sorts based on mode.
    Returns the top 'limit' candidates.
    """
    logger.info(f"🔍 Starting Bybit Spot scan (mode={mode}, limit={limit})...")
    exchange = ccxt.bybit({'options': {'defaultType': 'spot'}})
    
    try:
        exchange.load_markets()
        # fetch_tickers retrieves 24h ticker details for all symbols in a single call
        tickers = exchange.fetch_tickers()
    except Exception as e:
        logger.error(f"Failed to fetch tickers from Bybit: {e}")
        return []

    candidates = []
    for symbol, ticker in tickers.items():
        # Get market representation to ensure it is spot
        market = exchange.markets.get(symbol)
        if not market or not market.get("spot"):
            continue
            
        # Filter for Spot USDT pairs (support standard symbol and alternative CCXT unified format)
        base_symbol = symbol.split(":")[0] if ":" in symbol else symbol
        if not base_symbol.endswith("/USDT"):
            continue
            
        # Filter by watchlist/favourites
        if watchlist is not None:
            match_found = False
            for w in watchlist:
                if w.upper() in (symbol.upper(), base_symbol.upper(), base_symbol.replace("/", "").upper()):
                    match_found = True
                    break
            if not match_found:
                continue

        # Extract volume and change
        quote_volume = ticker.get("quoteVolume")
        percentage = ticker.get("percentage")
        
        # If quoteVolume is missing, estimate using baseVolume * close price
        if quote_volume is None:
            base_vol = ticker.get("baseVolume") or 0.0
            close_price = ticker.get("close") or 0.0
            quote_volume = base_vol * close_price
            
        # Require minimum liquidity of 1,000,000 USDT to avoid illiquid pump-and-dumps
        if quote_volume < 1_000_000.0:
            continue
            
        if percentage is None:
            continue
            
        candidates.append({
            "symbol": symbol,
            "volume": quote_volume,
            "change": percentage,
        })
        
    logger.info(f"Found {len(candidates)} liquid Bybit spot pairs.")
    
    # Sort based on strategy mode
    if mode == "oversold":
        # Sort ascending (biggest decliners first)
        candidates.sort(key=lambda x: x["change"])
    elif mode == "momentum":
        # Sort descending (biggest gainers first)
        candidates.sort(key=lambda x: x["change"], reverse=True)
    elif mode in ("volume", "hot"):
        # Sort descending (highest volume first)
        candidates.sort(key=lambda x: x["volume"], reverse=True)
        
    selected = [c["symbol"] for c in candidates[:limit]]
    logger.info(f"Selected Bybit candidates: {selected}")
    return selected


def scan_massive_stocks(mode: str = "oversold", limit: int = 5, watchlist: Optional[list[str]] = None) -> list[str]:
    """
    Scans US stocks using Massive Grouped Daily aggregates.
    Filters by price (> $5) and volume (> 500,000) and sorts based on mode.
    Returns the top 'limit' candidates.
    """
    api_key = settings.get_massive_api_key
    if not api_key:
        logger.warning("No Massive API key found. Skipping Stock scan.")
        return []
        
    try:
        date_str = get_latest_trading_date()
    except Exception as e:
        logger.error(f"Failed to resolve latest stock trading date: {e}")
        return []
        
    logger.info(f"🔍 Starting Massive Grouped Stocks scan for {date_str} (mode={mode}, limit={limit})...")
    url = f"https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/{date_str}"
    
    try:
        resp = get_with_retry(url, params={"adjusted": "true", "apiKey": api_key}, timeout=8)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as e:
        logger.error(f"Failed to fetch Grouped aggregates from Massive: {e}")
        return []
        
    candidates = []
    for r in results:
        ticker = r.get("T")
        open_p = r.get("o")
        close_p = r.get("c")
        vol = r.get("v")
        
        if not ticker or not open_p or not close_p or not vol:
            continue
            
        # Clean ticker format filtering (length 1 to 5, alphanumeric, avoids preferred shares/warrants)
        if len(ticker) > 5 or not ticker.isalnum():
            continue
            
        # Filter by watchlist/favourites
        if watchlist is not None and ticker.upper() not in [w.upper() for w in watchlist]:
            continue

        # Filter: Price > $5, Volume > 500,000
        if close_p < 5.0 or vol < 500_000:
            continue
            
        # Daily return percentage change
        pct_change = (close_p - open_p) / open_p * 100
        
        candidates.append({
            "ticker": ticker,
            "volume": vol * close_p,  # Traded dollar volume
            "change": pct_change,
        })
        
    logger.info(f"Found {len(candidates)} liquid stocks.")
    
    # Sort based on mode
    if mode == "oversold":
        candidates.sort(key=lambda x: x["change"])
    elif mode == "momentum":
        candidates.sort(key=lambda x: x["change"], reverse=True)
    elif mode in ("volume", "hot"):
        candidates.sort(key=lambda x: x["volume"], reverse=True)
        
    selected = [c["ticker"] for c in candidates[:limit]]
    logger.info(f"Selected stock candidates: {selected}")
    return selected


def fetch_orderbook_imbalance(symbol: str) -> float:
    """Calculates order book buy pressure imbalance (bids vs asks volume)."""
    is_crypto = "/" in symbol or symbol.endswith("USDT") or symbol.endswith("USD")
    try:
        if is_crypto:
            exchange = ccxt.bybit({'options': {'defaultType': 'spot'}})
            ob = exchange.fetch_order_book(symbol)
            bids = ob.get("bids", [])
            asks = ob.get("asks", [])
            bid_vol = sum([b[1] for b in bids[:20]])
            ask_vol = sum([a[1] for a in asks[:20]])
            if (bid_vol + ask_vol) > 0:
                return bid_vol / (bid_vol + ask_vol)
        else:
            # Stocks: fetch quote size from Alpaca
            from alpaca.data.historical import StockHistoricalDataClient
            from alpaca.data.requests import StockLatestQuoteRequest
            client = StockHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_secret_key)
            res = client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
            quote = res.get(symbol)
            if quote:
                bid_size = float(quote.bid_size)
                ask_size = float(quote.ask_size)
                if (bid_size + ask_size) > 0:
                    return bid_size / (bid_size + ask_size)
    except Exception as e:
        logger.warning(f"Error fetching order book imbalance for {symbol}: {e}")
    return 0.5


def rank_and_enrich_candidates(symbols: list[str], mode: str, limit: int) -> list[str]:
    """
    For a list of candidate symbols, builds snapshots, computes advanced math anomalies,
    scrapes social sentiment, and returns the top candidates sorted by combined anomaly score.
    """
    from trading_engine.data.market_data import build_snapshot
    from trading_engine.utils.scrapers import get_social_sentiment_context
    
    enriched = []
    for symbol in symbols:
        try:
            # Build snapshot (automatically fetches OHLCV & calculates technical metrics)
            snap = build_snapshot(symbol, settings.timeframe)
            
            # Fetch order book depth
            imbalance = fetch_orderbook_imbalance(symbol)
            snap.orderbook_imbalance = imbalance
            
            # Fetch news & social feeds (Twitter, Stocktwits, Google News)
            social_data = get_social_sentiment_context(symbol)
            snap.tweets = social_data.get("tweets", [])
            snap.stocktwits_raw = social_data.get("stocktwits_raw", "")
            snap.news_headlines = social_data.get("news_headlines", [])
            
            # Calculate Combined Anomaly Score
            # Base components: volume breakout and order book depth pressure
            vol_score = snap.rel_volume * 2.0
            book_score = imbalance * 5.0
            
            if mode == "oversold":
                # Lower RSI = stronger oversold anomaly
                rsi_score = max(0, 50 - snap.rsi) * 1.5
                anomaly_score = rsi_score + vol_score + book_score
            elif mode == "momentum":
                # Favor RSI between 50 and 75, penalize above 80 (exhaustion)
                if snap.rsi > 80:
                    rsi_score = max(0, 80 - (snap.rsi - 80)) * 1.5
                else:
                    rsi_score = max(0, snap.rsi - 50) * 1.5
                anomaly_score = rsi_score + (snap.rel_volume * 3.0) + book_score
            elif mode == "hot":
                # Hot score: combination of relative volume, orderbook imbalance, and social buzz frequency
                mention_count = len(snap.tweets)
                if snap.stocktwits_raw:
                    mention_count += len([line for line in snap.stocktwits_raw.split("\n") if line.strip()])
                mention_count += len(snap.news_headlines)
                
                anomaly_score = (snap.rel_volume * 3.0) + (imbalance * 4.0) + (mention_count * 2.0)
            else:  # volume breakout mode
                anomaly_score = (snap.rel_volume * 5.0) + book_score + abs(snap.rsi - 50)
                
            logger.info(f"📊 {symbol} anomaly check: RSI={snap.rsi:.1f} | RelVol={snap.rel_volume:.2f} | Imbalance={imbalance:.2f} -> Score: {anomaly_score:.2f}")
            enriched.append({
                "symbol": symbol,
                "anomaly_score": anomaly_score,
            })
        except Exception as e:
            logger.warning(f"Failed to enrich candidate {symbol}: {e}")
            
    # Sort descending by anomaly score
    enriched.sort(key=lambda x: x["anomaly_score"], reverse=True)
    return [item["symbol"] for item in enriched[:limit]]


def check_preflight_probability(snap: MarketSnapshot, mode: str) -> tuple[bool, float, str]:
    """
    Computes the pass probability (0.0 to 100.0) of a candidate.
    Returns (is_viable, probability, reason).
    A candidate is viable if it passes all hard vetoes and has a probability >= 75%.
    """
    close = snap.close
    atr = snap.atr
    bb_width = snap.bb_width
    
    # ── Hard Vetoes ───────────────────────────────────
    if atr <= 0 or close <= 0:
        return False, 0.0, "Veto: ATR or price is zero (bad data)"
        
    atr_pct = (atr / close) * 100
    if atr_pct > 8.0:
        return False, 0.0, f"Veto: Extreme volatility (ATR%={atr_pct:.1f}% > 8.0%)"
        
    if bb_width > 0.12:
        return False, 0.0, f"Veto: Market too volatile (BB width={bb_width:.3f} > 0.12)"
        
    stop_loss_pct = (1.5 * atr) / close
    if stop_loss_pct > 0.08:
        return False, 0.0, f"Veto: Stop loss too wide ({stop_loss_pct:.1%} > 8.0%)"
        
    if snap.realized_vol is not None and snap.realized_vol > 1.5:
        return False, 0.0, f"Veto: Extreme realized volatility ({snap.realized_vol:.2f} > 1.5)"

    # ── Probability Scoring ───────────────────────────
    probability = 50.0  # Base probability
    reasons = []

    # Volatility Check
    if 1.0 <= atr_pct <= 3.0:
        probability += 10.0
        reasons.append("Healthy volatility")
    elif atr_pct < 0.5:
        probability -= 10.0
        reasons.append("Low volatility/choppy")

    if 0.03 <= bb_width <= 0.09:
        probability += 10.0
        reasons.append("Good Bollinger Band squeeze/structure")

    # Mode-specific scoring
    if mode == "oversold":
        # RSI
        if snap.rsi <= 20:
            probability += 30.0
            reasons.append(f"Deep oversold RSI ({snap.rsi:.1f})")
        elif snap.rsi <= 30:
            probability += 20.0
            reasons.append(f"Oversold RSI ({snap.rsi:.1f})")
        elif snap.rsi <= 40:
            probability += 10.0
            reasons.append(f"Mildly oversold RSI ({snap.rsi:.1f})")
        elif snap.rsi >= 65:
            probability -= 35.0
            reasons.append(f"Overbought RSI ({snap.rsi:.1f})")

        # Stoch RSI
        if snap.stoch_rsi_k < 20 and snap.stoch_rsi_d < 20:
            probability += 15.0
            reasons.append("StochRSI oversold alignment")

        # Support Proximity
        if snap.df is not None and not snap.df.empty:
            recent_low = snap.df["low"].tail(20).min()
            if close <= recent_low * 1.02:
                probability += 15.0
                reasons.append("Near 20-period support")
                
        # Fear & Greed
        if snap.fear_greed_index is not None:
            if snap.fear_greed_index <= 25:
                probability += 15.0
                reasons.append("Fear & Greed extreme fear")
            elif snap.fear_greed_index <= 45:
                probability += 5.0
                reasons.append("Fear & Greed fear")
            elif snap.fear_greed_index >= 75:
                probability -= 20.0
                reasons.append("Fear & Greed greed")

    elif mode == "momentum":
        # Check trend direction first (buy vs sell)
        is_bullish = False
        if snap.df is not None and len(snap.df) >= 5:
            is_bullish = close > snap.df["close"].iloc[-5]
            
        if is_bullish:
            # Bullish trend alignment
            if snap.ema20 > snap.ema50 > snap.ema200:
                probability += 20.0
                reasons.append("Bullish EMA alignment (20>50>200)")
            elif snap.ema20 > snap.ema50:
                probability += 10.0
                reasons.append("Bullish short-term EMAs")
                
            if close > snap.ema200:
                probability += 10.0
                reasons.append("Price above EMA200")
                
            if snap.rel_volume >= 1.5:
                probability += 15.0
                reasons.append("High volume backing momentum")
                
            if snap.close > snap.vwap:
                probability += 10.0
                reasons.append("Price above VWAP")
                
            if 50 < snap.rsi <= 75:
                probability += 15.0
                reasons.append(f"Strong RSI momentum ({snap.rsi:.1f})")
            elif snap.rsi > 78:
                probability -= 35.0
                reasons.append("RSI overbought exhaustion")
        else:
            # Bearish trend alignment (short setup)
            if snap.ema20 < snap.ema50 < snap.ema200:
                probability += 20.0
                reasons.append("Bearish EMA alignment (20<50<200)")
            elif snap.ema20 < snap.ema50:
                probability += 10.0
                reasons.append("Bearish short-term EMAs")
                
            if close < snap.ema200:
                probability += 10.0
                reasons.append("Price below EMA200")
                
            if snap.rel_volume >= 1.5:
                probability += 15.0
                reasons.append("High volume backing selloff")
                
            if snap.close < snap.vwap:
                probability += 10.0
                reasons.append("Price below VWAP")
                
            if 25 <= snap.rsi < 50:
                probability += 15.0
                reasons.append(f"Bearish RSI momentum ({snap.rsi:.1f})")
            elif snap.rsi < 22:
                probability -= 35.0
                reasons.append("RSI oversold exhaustion")

    elif mode == "hot":
        if snap.rel_volume >= 2.0:
            probability += 25.0
            reasons.append("Heavy breakout volume")
        elif snap.rel_volume >= 1.5:
            probability += 15.0
            reasons.append("Above average volume")
            
        if snap.orderbook_imbalance >= 0.6:
            probability += 15.0
            reasons.append("Strong buy-side order book pressure")
        elif snap.orderbook_imbalance <= 0.4:
            probability += 15.0
            reasons.append("Strong sell-side order book pressure")
            
        # Social buzz count
        mention_count = len(snap.tweets)
        if snap.stocktwits_raw:
            mention_count += len([line for line in snap.stocktwits_raw.split("\n") if line.strip()])
        mention_count += len(snap.news_headlines)
        
        if mention_count >= 5:
            probability += 20.0
            reasons.append("High social/news discussion volume")
        elif mention_count >= 2:
            probability += 10.0
            reasons.append("Moderate social/news discussion")
            
        if bb_width <= 0.08:
            probability += 10.0
            reasons.append("Consolidation before expansion")

    elif mode == "volume":
        if snap.rel_volume >= 2.0:
            probability += 30.0
            reasons.append("Volume breakout (>2x average)")
            
        # VWAP alignment
        if snap.close > snap.vwap:
            probability += 15.0
            reasons.append("Above VWAP (bullish accumulation)")
        else:
            probability += 5.0
            reasons.append("Below VWAP (bearish distribution)")
            
        if 45 <= snap.rsi <= 70:
            probability += 15.0
            reasons.append("RSI in healthy trend zone")
            
        if bb_width >= 0.04:
            probability += 10.0
            reasons.append("Volatile breakout expansion")

    probability = max(0.0, min(100.0, probability))
    
    # Check if passes threshold
    is_viable = probability >= 75.0
    reason_str = " | ".join(reasons) if reasons else "Neutral conditions"
    if not is_viable:
        reason_str = f"Pass probability {probability:.1f}% below 75% threshold. Details: {reason_str}"
        
    return is_viable, probability, reason_str


def run_bounty_hunt(mode: str = "oversold", crypto_limit: int = 5, stock_limit: int = 5, watchlist: Optional[list[str]] = None) -> list[dict]:
    """
    Scans the market, picks top candidates, runs the multi-agent pipeline
    on each, and returns a detailed report.
    """
    logger.info("⚔️ Bounty Hunter Scan Cycle Triggered ⚔️")
    
    # Fast scan a larger pool in bulk (e.g. limit * 3) to allow math ranking
    raw_crypto = scan_bybit_crypto(mode=mode, limit=crypto_limit * 3, watchlist=watchlist)
    raw_stocks = scan_massive_stocks(mode=mode, limit=stock_limit * 3, watchlist=watchlist)
    
    logger.info("📐 Verifying anomalies and ranking candidates...")
    crypto_candidates = rank_and_enrich_candidates(raw_crypto, mode, limit=crypto_limit)
    stock_candidates = rank_and_enrich_candidates(raw_stocks, mode, limit=stock_limit)
    
    all_candidates = crypto_candidates + stock_candidates
    results = []
    
    for symbol in all_candidates:
        logger.info(f"Checking pre-flight checklist for candidate: {symbol} ...")
        try:
            from trading_engine.data.market_data import build_snapshot
            snap = build_snapshot(symbol, settings.timeframe)
            
            is_viable, probability, preflight_reason = check_preflight_probability(snap, mode)
            
            if not is_viable:
                logger.warning(f"⚠️ Pre-flight check rejected {symbol}: probability={probability:.1f}% | {preflight_reason}")
                results.append({
                    "symbol": symbol,
                    "final_action": "NO_TRADE",
                    "entry_price": snap.close,
                    "stop_loss": None,
                    "take_profit": None,
                    "position_size_usd": None,
                    "confidence": probability,
                    "agreement": 0,
                    "reasoning": f"Rejected by Bounty Hunter pre-flight checklist. {preflight_reason}"
                })
                continue

            logger.info(f"✅ Candidate {symbol} passed pre-flight checklist: probability={probability:.1f}%! Running deep analysis...")
            # Execute the full pipeline
            sig = run_pipeline(
                symbol=symbol,
                portfolio_heat=0.0,
                open_positions=0,
                win_rate=0.50
            )
            
            results.append({
                "symbol": symbol,
                "final_action": sig.final_action,
                "entry_price": sig.entry_price,
                "stop_loss": sig.stop_loss,
                "take_profit": sig.take_profit,
                "position_size_usd": sig.position_size_usd,
                "confidence": sig.verdict.get("confidence", 0.0),
                "agreement": sig.verdict.get("agreement", 0),
                "reasoning": sig.reasoning
            })
        except Exception as e:
            logger.error(f"Pipeline failed for candidate {symbol}: {e}")
            
    return results
