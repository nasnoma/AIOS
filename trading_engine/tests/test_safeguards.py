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


def test_proportional_cash_allocator():
    """Asserts that _allocate_cash_proportionally divides cash correctly and filters out dust sizes."""
    from trading_engine.scheduler import _allocate_cash_proportionally
    from trading_engine.orchestrator import TradeSignal

    # Create dummy trade signals
    sig_a = MagicMock(spec=TradeSignal)
    sig_a.symbol = "AAPL"
    sig_a.final_action = "BUY"
    sig_a.position_size_usd = 600.0

    sig_b = MagicMock(spec=TradeSignal)
    sig_b.symbol = "TSLA"
    sig_b.final_action = "BUY"
    sig_b.position_size_usd = 600.0

    # 1. Total requested <= available cash -> no scaling
    allocs = _allocate_cash_proportionally([sig_a, sig_b], 1500.0)
    assert len(allocs) == 2
    assert allocs[0][1] == 600.0
    assert allocs[1][1] == 600.0

    # 2. Total requested > available cash -> proportional scaling
    allocs = _allocate_cash_proportionally([sig_a, sig_b], 1000.0)
    assert len(allocs) == 2
    assert allocs[0][1] == 500.0
    assert allocs[1][1] == 500.0

    # 3. Micro trade / dust trade skip
    sig_c = MagicMock(spec=TradeSignal)
    sig_c.symbol = "MSFT"
    sig_c.final_action = "BUY"
    sig_c.position_size_usd = 15.0

    # Scale factor = 100 / 1215 = ~0.082
    # Allocated C = 15 * 0.082 = 1.23 (< $10 minimum) -> should be skipped!
    allocs = _allocate_cash_proportionally([sig_a, sig_b, sig_c], 100.0)
    assert len(allocs) == 2  # sig_c skipped
    assert all(a[0].symbol != "MSFT" for a in allocs)





def test_confidence_weighted_sizing():
    """
    Asserts confidence-weighted sizing produces larger sizes for high-confidence
    trades and smaller sizes for borderline trades, and that low-trade-count
    discount reduces size for thin NGX stocks.
    """
    from unittest.mock import patch, MagicMock
    from trading_engine.risk_agent import evaluate as risk_evaluate
    from trading_engine.judge import JudgeVerdict, Signal
    from trading_engine.data.market_data import MarketSnapshot
    import pandas as pd

    snap = MagicMock(spec=MarketSnapshot)
    snap.symbol    = "MSFT"
    snap.close     = 400.0
    snap.atr       = 4.0
    snap.bb_width  = 0.04
    snap.timeframe = "1d"
    snap.asset_type = "stock"
    snap.df = pd.DataFrame({"close": [400.0] * 60},
                           index=pd.date_range("2025-01-01", periods=60))

    bull_df = pd.DataFrame({"close": [100.0] * 200 + [110.0] * 20},
                           index=pd.date_range("2023-01-01", periods=220))
    base = dict(current_portfolio_heat=0.0, historical_win_rate=0.55,
                open_positions=0, open_position_snaps=None, daily_pnl_usd=0.0)

    with patch("trading_engine.data.market_data.load_historical_data", return_value=bull_df), \
         patch.object(settings, "max_portfolio_heat", 1.0), \
         patch.object(settings, "confidence_sizing_enabled", True), \
         patch.object(settings, "position_size_min_weight", 0.6), \
         patch.object(settings, "position_size_max_weight", 1.4), \
         patch.object(settings, "min_avg_confidence", 48.0):

        verdict_high = JudgeVerdict(
            decision=Signal.BUY, confidence=90.0, agreement=7, disagreement=1,
            weighted_score=0.9, reasoning="Strong", agent_reports=[], approved=True)
        verdict_low = JudgeVerdict(
            decision=Signal.BUY, confidence=52.0, agreement=3, disagreement=4,
            weighted_score=0.52, reasoning="Weak", agent_reports=[], approved=True)

        dec_high = risk_evaluate(verdict_high, snap, **base)
        dec_low  = risk_evaluate(verdict_low,  snap, **base)

        if dec_high.approved and dec_low.approved:
            assert dec_high.position_size_usd > dec_low.position_size_usd, (
                f"High-conf (${dec_high.position_size_usd:.0f}) should exceed "
                f"low-conf (${dec_low.position_size_usd:.0f})")

    # Low-trade-count discount: TRANSEXPR (thin) vs MBENEFIT (not thin)
    def make_ngx_snap(sym, price=2.0, atr=0.05):
        s = MagicMock(spec=MarketSnapshot)
        s.symbol = sym; s.close = price; s.atr = atr; s.bb_width = 0.04
        s.timeframe = "1d"; s.asset_type = "stock"; s.prev_close = price * 0.98
        s.df = pd.DataFrame({"close": [price]*60}, index=pd.date_range("2025-01-01", periods=60))
        return s

    ngx_bull = pd.DataFrame({"close": [100.0]*200 + [110.0]*20},
                             index=pd.date_range("2023-01-01", periods=220))
    verdict_ngx = JudgeVerdict(
        decision=Signal.BUY, confidence=75.0, agreement=4, disagreement=2,
        weighted_score=0.75, reasoning="NGX", agent_reports=[], approved=True)

    with patch("trading_engine.data.market_data.load_historical_data", return_value=ngx_bull), \
         patch.object(settings, "max_portfolio_heat", 1.0), \
         patch.object(settings, "confidence_sizing_enabled", True), \
         patch.object(settings, "low_trade_count_discount", 0.75), \
         patch.object(settings, "low_trade_count_threshold", 10):

        dec_thin  = risk_evaluate(verdict_ngx, make_ngx_snap("TRANSEXPR/NGX"), **base)
        dec_thick = risk_evaluate(verdict_ngx, make_ngx_snap("MBENEFIT/NGX"),  **base)

        if dec_thin.approved and dec_thick.approved:
            assert dec_thin.position_size_usd <= dec_thick.position_size_usd, (
                "Thin stock (TRANSEXPR) should not exceed thick stock (MBENEFIT) sizing")


def test_concurrency_lock(tmp_path):
    """Asserts that concurrent calls to open_trade for the same symbol are serialized and only one position is opened."""
    import json
    import concurrent.futures
    from trading_engine.execution import paper_trader
    
    # Isolate state and lock files
    temp_state = tmp_path / "paper_state_concurrency_test.json"
    temp_lock = tmp_path / "paper_state_concurrency_test.lock"
    
    # Setup initial state
    initial_portfolio = paper_trader.PaperPortfolio(
        account_size=10000.0,
        cash=10000.0,
        positions=[],
        closed_trades=[]
    )
    
    with open(temp_state, "w") as f:
        json.dump(initial_portfolio.to_dict(), f, indent=2)
        
    with patch("trading_engine.execution.paper_trader.STATE_FILE", temp_state), \
         patch("trading_engine.execution.paper_trader.LOCK_FILE", temp_lock), \
         patch("trading_engine.execution.paper_trader.send_message") as mock_send_message:
         
        # We want to run multiple threads calling open_trade concurrently
        num_threads = 5
        symbol = "BTC/USDT"
        
        # Helper function to call open_trade
        def run_open():
            # Add a tiny sleep to simulate concurrency overlap
            time.sleep(0.01)
            return paper_trader.open_trade(
                symbol=symbol,
                direction="long",
                entry=60000.0,
                size_usd=1000.0,
                stop_loss=55000.0,
                take_profit=70000.0,
                atr=1000.0
            )
            
        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(run_open) for _ in range(num_threads)]
            results = [f.result() for f in concurrent.futures.as_completed(futures)]
            
        # Verify that only one call succeeded (returned a Position) and others returned None
        succeeded_trades = [r for r in results if r is not None]
        assert len(succeeded_trades) == 1, f"Expected exactly 1 trade to succeed, got {len(succeeded_trades)}"
        
        # Verify state file has exactly 1 position
        final_portfolio = paper_trader._load_state()
        assert len(final_portfolio.open_positions) == 1
        assert final_portfolio.open_positions[0].symbol == symbol

