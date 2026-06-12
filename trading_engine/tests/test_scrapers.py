"""
trading_engine/tests/test_scrapers.py
Unit tests for social sentiment scraping functions.
"""
import pytest
from unittest.mock import patch, MagicMock
from trading_engine.utils import scrapers


class TestScrapers:
    @patch("requests.get")
    def test_fetch_google_news_success(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.content = b"""<rss version="2.0">
            <channel>
                <item><title>AAPL hits all time high</title></item>
                <item><title>Apple earnings release details</title></item>
            </channel>
        </rss>"""
        mock_get.return_value = mock_resp
        
        headlines = scrapers.fetch_google_news("AAPL")
        assert len(headlines) == 2
        assert headlines[0] == "AAPL hits all time high"
        assert headlines[1] == "Apple earnings release details"

    @patch("requests.post")
    @patch("requests.get")
    def test_scrape_twitter_apify_success(self, mock_get, mock_post):
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 201
        mock_post_resp.json.return_value = {"data": {"defaultDatasetId": "dataset_123"}}
        mock_post.return_value = mock_post_resp
        
        mock_get_resp = MagicMock()
        mock_get_resp.ok = True
        mock_get_resp.json.return_value = [
            {"text": "Bullish on $BTC!"},
            {"fullText": "Going to buy some more HOME token"}
        ]
        mock_get.return_value = mock_get_resp
        
        tweets = scrapers.scrape_twitter_apify("BTC", "mock_token")
        assert len(tweets) == 2
        assert tweets[0] == "Bullish on $BTC!"
        assert tweets[1] == "Going to buy some more HOME token"

    @patch("requests.post")
    def test_scrape_stocktwits_firecrawl_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "success": True,
            "data": {"markdown": "### StocktwitsAAPL\nSome post texts here"}
        }
        mock_post.return_value = mock_resp
        
        markdown = scrapers.scrape_stocktwits_firecrawl("AAPL", "mock_key")
        assert "StocktwitsAAPL" in markdown

    @patch("trading_engine.utils.scrapers.fetch_google_news")
    @patch("trading_engine.utils.scrapers.scrape_twitter_apify")
    @patch("trading_engine.utils.scrapers.scrape_stocktwits_firecrawl")
    @patch("os.getenv")
    def test_get_social_sentiment_context(self, mock_getenv, mock_firecrawl, mock_twitter, mock_news):
        mock_getenv.side_effect = lambda key: "mock_val" if key in ("APIFY_API_TOKEN", "FIRECRAWL_API_KEY") else None
        
        mock_news.return_value = ["News headline"]
        mock_twitter.return_value = ["Tweet context"]
        mock_firecrawl.return_value = "Stocktwits markdown context"
        
        context = scrapers.get_social_sentiment_context("AAPL")
        
        assert len(context["tweets"]) == 1
        assert context["stocktwits_raw"] == "Stocktwits markdown context"
        assert len(context["news_headlines"]) == 1
