import os
import pytest
from trading_engine.config import settings
from trading_engine.storage import db

@pytest.fixture(autouse=True)
def use_test_database():
    """Redirect database connection to SQLite and disable Telegram for tests."""
    # Reset cached engine/session in db.py
    db._engine = None
    db._SessionLocal = None
    
    # Temporarily override database_url to in-memory sqlite
    original_url = settings.database_url
    settings.database_url = "sqlite:///:memory:"
    os.environ["IS_TESTING"] = "true"
    
    # Temporarily clear Telegram config to avoid spamming the user during tests
    original_bot_token = settings.telegram_bot_token
    original_chat_id = settings.telegram_chat_id
    settings.telegram_bot_token = ""
    settings.telegram_chat_id = ""

    # Override engine execution parameters for test consistency
    original_regime = settings.regime_filter_enabled
    original_agreement = settings.min_agent_agreement
    original_confidence = settings.min_avg_confidence
    original_use_5m = settings.crypto_use_5m_atr
    original_let_runners = getattr(settings, "let_winners_run_enabled", True)
    settings.regime_filter_enabled = True
    settings.min_agent_agreement = 5
    settings.min_avg_confidence = 52.0
    settings.crypto_use_5m_atr = True
    # Disable "let winners run" in tests so fixed-TP assertions still hold
    settings.let_winners_run_enabled = False
    
    yield
    
    # Restore original settings
    settings.database_url = original_url
    os.environ.pop("IS_TESTING", None)
    settings.telegram_bot_token = original_bot_token
    settings.telegram_chat_id = original_chat_id
    settings.regime_filter_enabled = original_regime
    settings.min_agent_agreement = original_agreement
    settings.min_avg_confidence = original_confidence
    settings.crypto_use_5m_atr = original_use_5m
    settings.let_winners_run_enabled = original_let_runners
    db._engine = None
    db._SessionLocal = None
