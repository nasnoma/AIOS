"""
trading_engine/storage/db.py

SQLAlchemy-based database storage layer for audit logs.
Automatically falls back to local SQLite if configured PostgreSQL is unreachable.
"""
from __future__ import annotations
import json
import time
from datetime import datetime, timezone
from loguru import logger
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Text
from sqlalchemy.orm import declarative_base, sessionmaker
from trading_engine.config import settings

Base = declarative_base()

class APIAuditLog(Base):
    __tablename__ = "api_audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    endpoint = Column(String(255), nullable=False)
    request_method = Column(String(50))
    request_params = Column(Text)
    response_status = Column(Integer)
    response_payload = Column(Text)
    duration_ms = Column(Float)


class OrderAuditLog(Base):
    __tablename__ = "order_audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    symbol = Column(String(50), nullable=False)
    side = Column(String(20), nullable=False)
    qty = Column(Float, nullable=False)
    price = Column(Float, nullable=False)
    order_type = Column(String(50), default="market")
    payload = Column(Text)
    response = Column(Text)
    status = Column(String(50))  # 'success' | 'error'
    error_message = Column(Text)


# Connection setup with SQLite fallback
_engine = None
_SessionLocal = None

def get_engine():
    global _engine, _SessionLocal
    if _engine is not None:
        return _engine

    db_url = settings.database_url
    logger.info(f"Connecting to database: {db_url.split('@')[-1] if '@' in db_url else db_url}")

    try:
        # Check if it's postgresql and test connection
        if db_url.startswith("postgresql"):
            # Set a low connection timeout to fail fast if DB is down
            engine = create_engine(db_url, connect_args={"connect_timeout": 3})
            # Test connection
            with engine.connect() as conn:
                pass
            _engine = engine
            logger.info("Successfully connected to PostgreSQL database.")
        else:
            _engine = create_engine(db_url)
    except Exception as e:
        logger.warning(
            f"Failed to connect to PostgreSQL database ({e}). "
            f"Falling back to local SQLite database: sqlite:///trading_engine.db"
        )
        # Fallback to local SQLite in trading_engine parent folder
        import os
        from pathlib import Path
        db_path = Path(__file__).parent.parent.parent / "trading_engine.db"
        sqlite_url = f"sqlite:///{db_path}"
        _engine = create_engine(sqlite_url, connect_args={"timeout": 10})

    # Create tables
    try:
        Base.metadata.create_all(_engine)
    except Exception as e:
        logger.error(f"Error creating database tables: {e}")

    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)
    return _engine


def get_session():
    get_engine()
    if _SessionLocal is None:
        raise RuntimeError("Database session maker is not initialized.")
    return _SessionLocal()


def log_api_call(endpoint: str, method: str, params: dict | str | None,
                 status_code: int | None, response: dict | str | None,
                 duration_ms: float):
    """Logs an external API call to the database securely without throwing exceptions."""
    try:
        req_str = json.dumps(params, default=str) if isinstance(params, (dict, list)) else str(params or "")
        resp_str = json.dumps(response, default=str) if isinstance(response, (dict, list)) else str(response or "")
        
        with get_session() as session:
            log = APIAuditLog(
                endpoint=endpoint,
                request_method=method,
                request_params=req_str,
                response_status=status_code,
                response_payload=resp_str,
                duration_ms=duration_ms
            )
            session.add(log)
            session.commit()
    except Exception as e:
        logger.warning(f"Database failed to log API call to {endpoint}: {e}")


def log_order(symbol: str, side: str, qty: float, price: float,
              order_type: str, payload: dict | str | None,
              response: dict | str | None, status: str,
              error_message: str | None = None):
    """Logs an order execution payload to the database securely."""
    try:
        pay_str = json.dumps(payload, default=str) if isinstance(payload, (dict, list)) else str(payload or "")
        resp_str = json.dumps(response, default=str) if isinstance(response, (dict, list)) else str(response or "")
        
        with get_session() as session:
            log = OrderAuditLog(
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                order_type=order_type,
                payload=pay_str,
                response=resp_str,
                status=status,
                error_message=error_message
            )
            session.add(log)
            session.commit()
    except Exception as e:
        logger.warning(f"Database failed to log order for {symbol}: {e}")
