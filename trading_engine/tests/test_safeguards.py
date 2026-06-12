import pytest
from unittest.mock import patch, MagicMock
import time
from sqlalchemy import select
import ccxt

from trading_engine.config import settings
from trading_engine.storage import db
from trading_engine.execution.live_trader import retry_and_log_order

def test_db_fallback_to_sqlite():
    """Asserts that database manager automatically falls back to SQLite on connection error."""
    # Temporarily set database_url to an unreachable postgres address
    original_url = settings.database_url
    settings.database_url = "postgresql://trader_bad:secret_bad@localhost:9999/does_not_exist"
    
    # Force re-initialization of engine
    db._engine = None
    db._SessionLocal = None
    
    try:
        engine = db.get_engine()
        # Verify the fallback engine is SQLite
        assert "sqlite" in str(engine.url)
    finally:
        # Reset settings
        settings.database_url = original_url
        db._engine = None
        db._SessionLocal = None


def test_api_audit_logging():
    """Asserts that db.log_api_call writes audit log entries successfully."""
    # Force local SQLite connection for test
    db._engine = None
    db._SessionLocal = None
    original_url = settings.database_url
    settings.database_url = "sqlite:///:memory:"
    
    try:
        session = db.get_session()
        
        # Log a dummy API call
        db.log_api_call(
            endpoint="test_api_endpoint",
            method="GET",
            params={"param1": "val1"},
            status_code=200,
            response={"status": "ok"},
            duration_ms=45.2
        )
        
        # Query the database
        with db.get_session() as session:
            logs = session.query(db.APIAuditLog).all()
            assert len(logs) > 0
            latest = logs[-1]
            assert latest.endpoint == "test_api_endpoint"
            assert latest.request_method == "GET"
            assert "param1" in latest.request_params
            assert latest.response_status == 200
            assert "ok" in latest.response_payload
            assert latest.duration_ms == 45.2
            
    finally:
        settings.database_url = original_url
        db._engine = None
        db._SessionLocal = None


def test_order_audit_logging():
    """Asserts that db.log_order writes order payload entries successfully."""
    db._engine = None
    db._SessionLocal = None
    original_url = settings.database_url
    settings.database_url = "sqlite:///:memory:"
    
    try:
        session = db.get_session()
        
        db.log_order(
            symbol="BTC/USDT",
            side="buy",
            qty=0.05,
            price=65000.0,
            order_type="market",
            payload={"symbol": "BTC/USDT", "side": "buy"},
            response={"id": "order_123", "status": "filled"},
            status="success"
        )
        
        with db.get_session() as session:
            orders = session.query(db.OrderAuditLog).all()
            assert len(orders) > 0
            latest = orders[-1]
            assert latest.symbol == "BTC/USDT"
            assert latest.side == "buy"
            assert latest.qty == 0.05
            assert latest.price == 65000.0
            assert "order_123" in latest.response
            assert latest.status == "success"
            
    finally:
        settings.database_url = original_url
        db._engine = None
        db._SessionLocal = None


def test_retry_on_network_failure():
    """Asserts that retry_and_log_order retries on CCXT NetworkError and eventually succeeds."""
    mock_func = MagicMock()
    # First two attempts raise CCXT NetworkError, third attempt succeeds
    mock_func.side_effect = [
        ccxt.NetworkError("Rate limit hit or socket closed"),
        ccxt.NetworkError("Socket closed"),
        {"id": "order_789", "status": "closed", "average": 65000.0}
    ]
    
    # We mock time.sleep so the test runs fast without waiting
    with patch("time.sleep") as mock_sleep:
        # Force SQLite database to verify logging does not crash
        db._engine = None
        db._SessionLocal = None
        original_url = settings.database_url
        settings.database_url = "sqlite:///:memory:"
        
        try:
            result = retry_and_log_order(
                symbol="BTC/USDT",
                side="buy",
                qty=0.1,
                price=65000.0,
                order_type="market",
                exchange_func=mock_func
            )
            
            assert result["id"] == "order_789"
            assert mock_func.call_count == 3
            assert mock_sleep.call_count == 2
            
        finally:
            settings.database_url = original_url
            db._engine = None
            db._SessionLocal = None
