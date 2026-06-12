"""
trading_engine/utils/scrapers.py

Social media and news scrapers for candidate enrichment.
Supports Apify (Twitter), Firecrawl (Stocktwits crawling), and Google News RSS.
"""
from __future__ import annotations
import os
import requests
import xml.etree.ElementTree as ET
from loguru import logger

from trading_engine.config import settings


def fetch_google_news(symbol: str) -> list[str]:
    """
    Fetches the latest news headlines for a symbol from Google News RSS.
    Returns a list of title strings.
    """
    logger.info(f"📰 Fetching RSS news headlines for {symbol}...")
    url = f"https://news.google.com/rss/search?q={symbol}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.ok:
            root = ET.fromstring(resp.content)
            titles = []
            for item in root.findall(".//item")[:10]:
                title = item.find("title").text
                if title:
                    titles.append(title)
            return titles
    except Exception as e:
        logger.warning(f"Google News RSS fetch failed for {symbol}: {e}")
    return []


def scrape_twitter_apify(symbol: str, token: str) -> list[str]:
    """
    Triggers Apify Twitter Scraper to fetch recent tweets mentioning the symbol.
    Returns a list of tweet text strings.
    """
    logger.info(f"🐦 Triggering Apify Twitter Scraper for {symbol}...")
    # Using the popular apify/twitter-scraper actor
    run_url = f"https://api.apify.com/v2/acts/apify~twitter-scraper/run?token={token}"
    payload = {
        "searchTerms": [f"${symbol}"],
        "maxItems": 10,
        "tweetsDesired": 10,
        "onlyPost": True
    }
    try:
        # Trigger the actor and wait for completion (timeout after 15s)
        resp = requests.post(run_url, json=payload, timeout=20)
        if resp.status_code in (200, 201):
            run_data = resp.json().get("data", {})
            dataset_id = run_data.get("defaultDatasetId")
            if dataset_id:
                # Fetch dataset items
                items_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={token}"
                items_resp = requests.get(items_url, timeout=10)
                if items_resp.ok:
                    items = items_resp.json()
                    tweets = [item.get("fullText") or item.get("text") for item in items if item.get("fullText") or item.get("text")]
                    return tweets
        else:
            logger.warning(f"Apify returned status code {resp.status_code}")
    except Exception as e:
        logger.warning(f"Apify Twitter scrape failed for {symbol}: {e}")
    return []


def scrape_stocktwits_firecrawl(symbol: str, api_key: str) -> str:
    """
    Uses Firecrawl to scrape Stocktwits stream for a stock symbol.
    Returns the scraped markdown content.
    """
    logger.info(f"🔥 Scraping Stocktwits page for {symbol} via Firecrawl...")
    target_url = f"https://stocktwits.com/symbol/{symbol}"
    firecrawl_url = "https://api.firecrawl.dev/v1/scrape"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "url": target_url,
        "formats": ["markdown"]
    }
    try:
        resp = requests.post(firecrawl_url, json=payload, headers=headers, timeout=15)
        if resp.ok:
            data = resp.json()
            if data.get("success"):
                return data.get("data", {}).get("markdown", "")
        else:
            logger.warning(f"Firecrawl returned status code {resp.status_code}")
    except Exception as e:
        logger.warning(f"Firecrawl Stocktwits scrape failed for {symbol}: {e}")
    return ""


def get_social_sentiment_context(symbol: str) -> dict:
    """
    Enriches a candidate symbol with real social sentiment feeds.
    Checks environment for API tokens, using Google News RSS as the default fallback.
    """
    apify_token = os.getenv("APIFY_API_TOKEN") or getattr(settings, "apify_api_token", "")
    firecrawl_key = os.getenv("FIRECRAWL_API_KEY") or getattr(settings, "firecrawl_api_key", "")
    
    tweets = []
    stocktwits_raw = ""
    news = fetch_google_news(symbol)
    
    if apify_token:
        tweets = scrape_twitter_apify(symbol, apify_token)
        
    if firecrawl_key:
        stocktwits_raw = scrape_stocktwits_firecrawl(symbol, firecrawl_key)
        
    # Return collected feed data
    return {
        "tweets": tweets[:10],
        "stocktwits_raw": stocktwits_raw[:3000],  # Limit length
        "news_headlines": news[:10]
    }
