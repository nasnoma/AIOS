import pytest
from trading_engine.config import settings
from trading_engine.storage import db

@pytest.fixture(autouse=True)
def use_test_database():
    """Redirect database connection to an in-memory SQLite database for all unit tests."""
    # Reset cached engine/session in db.py
    db._engine = None
    db._SessionLocal = None
    
    # Temporarily override database_url to in-memory sqlite
    original_url = settings.database_url
    settings.database_url = "sqlite:///:memory:"
    
    yield
    
    # Restore original database_url and reset cached engine/session
    settings.database_url = original_url
    db._engine = None
    db._SessionLocal = None
