"""
polymarket_bot/market.py

Polymarket market discovery via Gamma API.
Handles:
  - Deterministic 5-minute window timing (300s Unix clock).
  - Fetching active YES/NO token IDs for BTC/ETH/SOL markets.
  - Caching token IDs per window to avoid redundant API calls.

Market title format: "Bitcoin Up or Down - June 29, 8:10AM-8:15AM ET"
Slug format: "btc-updown-5m-<window_end_unix_timestamp>"
  - Up (YES) token  = clobTokenIds[0]
  - Down (NO) token = clobTokenIds[1]
"""
from __future__ import annotations
import asyncio
import json
import time
from dataclasses import dataclass
from typing import Optional
import aiohttp
from loguru import logger

GAMMA_BASE = "https://gamma-api.polymarket.com"

# Asset slug prefix used in Polymarket's deterministic market slugs
_ASSET_SLUG = {
    "BTC": "btc",
    "ETH": "eth",
    "SOL": "sol",
    "BNB": "bnb",
    "DOGE": "doge",
    "XRP": "xrp",
}

# Fallback: search terms for the /markets question field
_ASSET_SEARCH = {
    "BTC": "Bitcoin Up or Down",
    "ETH": "Ethereum Up or Down",
    "SOL": "Solana Up or Down",
}

# In-memory cache: window_end_ts → {asset: MarketTokens}
_market_cache: dict[int, dict[str, "MarketTokens"]] = {}
_cache_lock = asyncio.Lock()


@dataclass
class MarketTokens:
    """UP and DOWN token IDs for a specific asset's 5-min market."""
    asset: str
    condition_id: str
    token_id_up: str    # UP / YES token (outcomes[0])
    token_id_down: str  # DOWN / NO token (outcomes[1])
    window_start: int   # Unix timestamp
    window_end: int     # Unix timestamp


@dataclass
class WindowInfo:
    """Timing information for the current 5-minute prediction window."""
    window_start: int    # Unix timestamp (seconds)
    window_end: int      # Unix timestamp (seconds)
    elapsed_s: float     # Seconds elapsed since window start
    remaining_s: float   # Seconds remaining in this window

    @property
    def progress_pct(self) -> float:
        return self.elapsed_s / 300.0 * 100


def get_current_window() -> WindowInfo:
    """
    Compute the active 5-minute window from the current UTC timestamp.
    Deterministic: window_start = now - (now % 300)
    """
    now = time.time()
    window_start = int(now) - (int(now) % 300)
    window_end = window_start + 300
    elapsed = now - window_start
    remaining = window_end - now
    return WindowInfo(
        window_start=window_start,
        window_end=window_end,
        elapsed_s=round(elapsed, 2),
        remaining_s=round(remaining, 2),
    )


async def _fetch_market_by_slug(
    session: aiohttp.ClientSession,
    asset: str,
    window_end: int,
) -> Optional[MarketTokens]:
    """
    PRIMARY method: construct the deterministic slug and fetch directly.
    Slug format: "{asset_slug}-updown-5m-{window_end}"
    e.g. "btc-updown-5m-1782735300"
    """
    asset_slug = _ASSET_SLUG.get(asset.upper())
    if not asset_slug:
        return None

    slug = f"{asset_slug}-updown-5m-{window_end}"
    url = f"{GAMMA_BASE}/markets"
    params = {"slug": slug}
    headers = {"Accept": "application/json", "Accept-Encoding": "gzip, deflate"}

    try:
        async with session.get(url, params=params, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()

        markets = data if isinstance(data, list) else [data]
        for m in markets:
            if not m or m.get("closed"):
                continue
            raw_token_ids = m.get("clobTokenIds", "[]")
            token_ids = json.loads(raw_token_ids) if isinstance(raw_token_ids, str) else raw_token_ids
            if len(token_ids) >= 2:
                ws = int(window_end) - 300
                logger.info(
                    f"  📍 {asset} market found via slug | "
                    f"UP={token_ids[0][:10]}... DOWN={token_ids[1][:10]}... "
                    f"| liquidity=${m.get('liquidityNum', m.get('liquidity', 0)):.0f}"
                )
                return MarketTokens(
                    asset=asset,
                    condition_id=m.get("conditionId", ""),
                    token_id_up=token_ids[0],
                    token_id_down=token_ids[1],
                    window_start=ws,
                    window_end=window_end,
                )
    except Exception as e:
        logger.debug(f"Slug lookup failed for {slug}: {e}")
    return None


async def _fetch_market_by_search(
    session: aiohttp.ClientSession,
    asset: str,
    window_end: int,
) -> Optional[MarketTokens]:
    """
    FALLBACK method: search /markets by question text, find the one
    whose endDate matches the current window.
    """
    search_q = _ASSET_SEARCH.get(asset.upper(), f"{asset} Up or Down")
    url = f"{GAMMA_BASE}/markets"
    params = {
        "limit": 50,
        "order": "createdAt",
        "ascending": "false",
        "active": "true",
        "closed": "false",
    }
    headers = {"Accept": "application/json", "Accept-Encoding": "gzip, deflate"}

    try:
        async with session.get(url, params=params, headers=headers,
                               timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status != 200:
                logger.warning(f"Gamma API returned {resp.status} for {asset} search")
                return None
            data = await resp.json()

        markets = data if isinstance(data, list) else data.get("data", [])

        # window_end is our target endDate (in unix). Match by endDate within ±5min
        target_end = window_end
        for m in markets:
            q = m.get("question", "")
            if search_q not in q:
                continue
            end_str = m.get("endDate", "")
            if not end_str:
                continue
            try:
                from datetime import datetime, timezone
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                m_end_ts = int(end_dt.timestamp())
                if abs(m_end_ts - target_end) > 300:
                    continue  # Wrong window
            except Exception:
                continue

            raw_token_ids = m.get("clobTokenIds", "[]")
            token_ids = json.loads(raw_token_ids) if isinstance(raw_token_ids, str) else raw_token_ids
            if len(token_ids) >= 2:
                ws = int(window_end) - 300
                logger.info(
                    f"  📍 {asset} market found via search | "
                    f"UP={token_ids[0][:10]}... DOWN={token_ids[1][:10]}..."
                )
                return MarketTokens(
                    asset=asset,
                    condition_id=m.get("conditionId", ""),
                    token_id_up=token_ids[0],
                    token_id_down=token_ids[1],
                    window_start=ws,
                    window_end=window_end,
                )

    except asyncio.TimeoutError:
        logger.warning(f"Gamma API timeout for {asset} search")
    except Exception as e:
        logger.warning(f"Gamma API error for {asset} search: {e}")

    logger.warning(f"No active 5-min market found for {asset}")
    return None


async def _fetch_market(
    session: aiohttp.ClientSession,
    asset: str,
    window_end: int,
) -> Optional[MarketTokens]:
    """Try slug first, fallback to search. Retry each up to 3 times with backoff on failure."""
    for attempt in range(3):
        result = await _fetch_market_by_slug(session, asset, window_end)
        if result:
            return result
        if attempt < 2:
            await asyncio.sleep(0.5 * (attempt + 1))

    for attempt in range(3):
        result = await _fetch_market_by_search(session, asset, window_end)
        if result:
            return result
        if attempt < 2:
            await asyncio.sleep(0.5 * (attempt + 1))

    return None


async def get_active_markets(assets: list[str]) -> dict[str, Optional[MarketTokens]]:
    """
    Returns a dict of {asset: MarketTokens} for the current 5-minute window.
    Results are cached per window to avoid redundant Gamma API calls.
    Only successful lookups are cached to allow retries on API index delay.
    """
    window = get_current_window()
    we = window.window_end

    async with _cache_lock:
        # Purge old window caches
        for old_we in list(_market_cache.keys()):
            if old_we < we:
                del _market_cache[old_we]

        cached = _market_cache.get(we, {})
        missing = [a for a in assets if a not in cached]

        if missing:
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            async with aiohttp.ClientSession(headers=headers) as session:
                tasks = [_fetch_market(session, a, we) for a in missing]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for asset, result in zip(missing, results):
                    if isinstance(result, Exception):
                        logger.error(f"Market discovery failed for {asset}: {result}")
                    elif result is not None:
                        cached[asset] = result
            _market_cache[we] = cached

        return {a: cached.get(a) for a in assets}
