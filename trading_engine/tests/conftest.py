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
    
    yield
    
    # Restore original settings
    settings.database_url = original_url
    os.environ.pop("IS_TESTING", None)
    settings.telegram_bot_token = original_bot_token
    settings.telegram_chat_id = original_chat_id
    db._engine = None
    db._SessionLocal = None
