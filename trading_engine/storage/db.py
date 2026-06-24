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


class ClosedTradeLog(Base):
    __tablename__ = "closed_trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), nullable=False)
    direction = Column(String(20), nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=False)
    size_usd = Column(Float, nullable=False)
    pnl_usd = Column(Float)
    fee_usd = Column(Float)
    opened_at = Column(DateTime, nullable=False)
    closed_at = Column(DateTime, nullable=False)
    exit_reason = Column(String(50))  # 'closed' | 'stopped'


class SymbolState(Base):
    __tablename__ = "symbol_states"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(50), unique=True, nullable=False)
    weights = Column(Text)  # JSON-serialized weights dict
    last_optimized_at = Column(DateTime)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


class PortfolioState(Base):
    __tablename__ = "portfolio_states"

    key = Column(String(50), primary_key=True)
    state_json = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)


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


def _parse_iso_to_naive_utc(iso_str: str | None) -> datetime | None:
    if not iso_str:
        return None
    try:
        clean_str = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_str)
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception as e:
        logger.warning(f"Failed to parse datetime string {iso_str}: {e}")
        return None


def get_db_closed_trades() -> list[dict]:
    """Retrieves all closed trades from the database as dictionaries, sorted by closed_at ascending."""
    trades = []
    try:
        with get_session() as session:
            db_trades = session.query(ClosedTradeLog).order_by(ClosedTradeLog.closed_at.asc()).all()
            for t in db_trades:
                opened_str = t.opened_at.isoformat()
                if "+" not in opened_str and "Z" not in opened_str:
                    opened_str += "+00:00"
                closed_str = t.closed_at.isoformat()
                if "+" not in closed_str and "Z" not in closed_str:
                    closed_str += "+00:00"
                
                trades.append({
                    "symbol": t.symbol,
                    "direction": t.direction,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "size_usd": t.size_usd,
                    "pnl_usd": t.pnl_usd,
                    "fee_usd": t.fee_usd,
                    "opened_at": opened_str,
                    "closed_at": closed_str,
                    "status": t.exit_reason,
                    "stop_loss": 0.0,
                    "take_profit": 0.0,
                    "atr": 0.0
                })
    except Exception as e:
        logger.warning(f"Database failed to load closed trades: {e}")
    return trades


def sync_closed_trades_to_db(closed_trades: list):
    """Syncs a list of closed trades (Position dataclass instances) to the database."""
    try:
        with get_session() as session:
            for t in closed_trades:
                opened_dt = _parse_iso_to_naive_utc(getattr(t, "opened_at", None))
                closed_dt = _parse_iso_to_naive_utc(getattr(t, "closed_at", None))
                
                if not opened_dt or not closed_dt:
                    continue
                
                # Check for existing trade in DB using symbol, direction, opened_at, and closed_at
                existing = session.query(ClosedTradeLog).filter_by(
                    symbol=t.symbol,
                    direction=t.direction,
                    opened_at=opened_dt,
                    closed_at=closed_dt
                ).first()
                
                if not existing:
                    db_trade = ClosedTradeLog(
                        symbol=t.symbol,
                        direction=t.direction,
                        entry_price=float(t.entry_price),
                        exit_price=float(t.exit_price) if t.exit_price is not None else 0.0,
                        size_usd=float(t.size_usd),
                        pnl_usd=float(t.pnl_usd) if t.pnl_usd is not None else 0.0,
                        fee_usd=float(t.fee_usd) if t.fee_usd is not None else 0.0,
                        opened_at=opened_dt,
                        closed_at=closed_dt,
                        exit_reason=getattr(t, "status", "closed")
                    )
                    session.add(db_trade)
            session.commit()
    except Exception as e:
        logger.warning(f"Database failed to sync closed trades: {e}")


def save_symbol_state(symbol: str, weights: dict | None = None, last_optimized_at: datetime | None = None):
    """Saves or updates the weights and/or last_optimized_at timestamp for a symbol in the database."""
    try:
        symbol_upper = symbol.upper().strip()
        with get_session() as session:
            state = session.query(SymbolState).filter_by(symbol=symbol_upper).first()
            if not state:
                state = SymbolState(symbol=symbol_upper)
                session.add(state)
            
            if weights is not None:
                state.weights = json.dumps(weights)
            if last_optimized_at is not None:
                # Remove timezone if naive, convert to naive UTC
                if last_optimized_at.tzinfo is not None:
                    last_optimized_at = last_optimized_at.astimezone(timezone.utc).replace(tzinfo=None)
                state.last_optimized_at = last_optimized_at
            
            state.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.commit()
            logger.info(f"Database: Saved state for {symbol_upper} (has_weights={weights is not None}, last_optimized_at={last_optimized_at})")
    except Exception as e:
        logger.warning(f"Failed to save symbol state for {symbol} to DB: {e}")


def get_symbol_state(symbol: str) -> dict | None:
    """Retrieves the symbol state from the database. Returns a dict with 'weights' and 'last_optimized_at'."""
    try:
        symbol_upper = symbol.upper().strip()
        with get_session() as session:
            state = session.query(SymbolState).filter_by(symbol=symbol_upper).first()
            if state:
                weights = None
                if state.weights:
                    try:
                        weights = json.loads(state.weights)
                    except Exception:
                        pass
                
                # Make last_optimized_at timezone aware (UTC) if present
                last_opt = state.last_optimized_at
                if last_opt:
                    last_opt = last_opt.replace(tzinfo=timezone.utc)
                    
                return {
                    "weights": weights,
                    "last_optimized_at": last_opt
                }
    except Exception as e:
        logger.warning(f"Failed to get symbol state for {symbol} from DB: {e}")
    return None


def save_portfolio_state(key: str, state: dict):
    """Saves or updates the serialized portfolio state in the database."""
    try:
        with get_session() as session:
            db_state = session.query(PortfolioState).filter_by(key=key).first()
            if not db_state:
                db_state = PortfolioState(key=key)
                session.add(db_state)
            db_state.state_json = json.dumps(state)
            db_state.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            session.commit()
            logger.info(f"Database: Saved portfolio state for key '{key}'")
    except Exception as e:
        logger.warning(f"Failed to save portfolio state for {key} to DB: {e}")


def get_portfolio_state(key: str) -> dict | None:
    """Retrieves the serialized portfolio state from the database."""
    try:
        with get_session() as session:
            db_state = session.query(PortfolioState).filter_by(key=key).first()
            if db_state and db_state.state_json:
                return json.loads(db_state.state_json)
    except Exception as e:
        logger.warning(f"Failed to get portfolio state for {key} from DB: {e}")
    return None

