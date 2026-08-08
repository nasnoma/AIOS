"""
trading_engine/spot/runner.py

Main orchestrator for spot grid and DCA trading.
Links RegimeDetector, SpotPortfolio, GridEngine, and DCAManager.
"""
from __future__ import annotations
import os
import json
import ccxt
from datetime import datetime, timezone
from loguru import logger
from typing import Dict, Any

from trading_engine.config import spot_settings, settings
from trading_engine.spot.regime_detector import RegimeDetector
from trading_engine.spot.spot_portfolio import SpotPortfolio
from trading_engine.spot.grid_engine import GridEngine
from trading_engine.spot.dca_manager import DCAManager

_portfolio = SpotPortfolio()
_regime_detectors: Dict[str, RegimeDetector] = {}
_grid_engines: Dict[str, GridEngine] = {}
_dca_manager = DCAManager()
_exchange: ccxt.Exchange | None = None


def get_spot_exchange() -> ccxt.Exchange:
    """Get Bybit spot exchange. In paper mode, uses public endpoints (no auth needed)."""
    global _exchange
    if _exchange is None:
        if not spot_settings.paper_mode and settings.bybit_api_key and settings.bybit_api_secret:
            # Live mode: use authenticated exchange
            _exchange = ccxt.bybit({
                'apiKey': settings.bybit_api_key,
                'secret': settings.bybit_api_secret,
                'options': {'defaultType': 'spot'},
                'enableRateLimit': True,
            })
            if settings.bybit_demo_trading:
                _exchange.set_sandbox_mode(True)
            logger.info("Spot engine: Bybit LIVE/DEMO mode (authenticated)")
        else:
            # Paper mode: public endpoints only (price data, no auth needed)
            _exchange = ccxt.bybit({
                'options': {'defaultType': 'spot'},
                'enableRateLimit': True,
            })
            logger.info("Spot engine: PAPER mode (public endpoints, simulated fills)")
    return _exchange


def init_spot_engine():
    """Initialise portfolio and grid engines for all configured spot assets."""
    _portfolio.load()
    
    # Calculate active total capital allocated (20% of account size)
    account_size = settings.account_size
    spot_active_capital = account_size * spot_settings.total_capital_pct  # e.g., 20% of $211K = ~$42K
    
    # Recalibrate portfolio cash to full $211,028.58 account size
    expected_free = spot_active_capital * (1.0 - spot_settings.usdt_hard_reserve_pct)
    if (_portfolio.usdt_available < 10000.0) and len(_portfolio.holdings) == 0:
        _portfolio.usdt_available = expected_free
        _portfolio.usdt_reserved = spot_active_capital * spot_settings.usdt_hard_reserve_pct
        _portfolio.save()



    exchange = get_spot_exchange()
    allocations = spot_settings.asset_allocation  # e.g. {'BTC/USDT': 0.45, ...}
    
    for symbol in spot_settings.asset_list:
        if symbol not in _regime_detectors:
            _regime_detectors[symbol] = RegimeDetector(symbol=symbol, timeframe=spot_settings.regime_timeframe)
        
        alloc_pct = allocations.get(symbol, 0.33)
        asset_usd = spot_active_capital * alloc_pct
        
        if symbol not in _grid_engines:
            _grid_engines[symbol] = GridEngine(
                symbol=symbol,
                allocated_usd=asset_usd,
                paper_mode=spot_settings.paper_mode,
                fee_rate=spot_settings.fee_rate
            )
            
    logger.info(f"✅ Spot Engine Initialised (Paper Mode: {spot_settings.paper_mode}, Active Capital: ${spot_active_capital:,.2f})")


def run_spot_regime_check():
    """Run regime detection for all symbols."""
    exchange = get_spot_exchange()
    for symbol in spot_settings.asset_list:
        detector = _regime_detectors.get(symbol)
        if not detector:
            detector = RegimeDetector(symbol=symbol)
            _regime_detectors[symbol] = detector
            
        try:
            state = detector.detect(exchange)
            engine = _grid_engines.get(symbol)
            if engine:
                engine.set_regime(state.regime.name)
            logger.info(f"📊 Regime [{symbol}]: {state.regime.name} (ADX: {state.adx:.1f}, +DI: {state.plus_di:.1f}, -DI: {state.minus_di:.1f})")
        except Exception as e:
            logger.warning(f"Failed regime check for {symbol}: {e}")


def run_spot_grid_tick() -> Dict[str, Any]:
    """
    Main spot tick job — fetches latest prices, updates portfolio, checks limit fills,
    and places new grid orders.
    """
    exchange = get_spot_exchange()
    _portfolio.reset_daily_if_needed()
    
    fill_events = []
    asset_list = spot_settings.asset_list
    
    # ── Bulk Ticker Fetch (Fast & Rate-Limit Resilient) ──
    tickers = {}
    try:
        tickers = exchange.fetch_tickers(asset_list)
    except Exception as e_bulk:
        logger.debug(f"Bulk ticker fetch failed ({e_bulk}), falling back to individual fetches")
        for sym in asset_list:
            try:
                tickers[sym] = exchange.fetch_ticker(sym)
            except Exception:
                pass
    
    for symbol in asset_list:
        engine = _grid_engines.get(symbol)
        if not engine:
            continue
            
        try:
            ticker = tickers.get(symbol)
            if not ticker or "last" not in ticker or not ticker["last"]:
                try:
                    ticker = exchange.fetch_ticker(symbol)
                except Exception:
                    continue
                    
            price = float(ticker["last"])
            _portfolio.update_price(symbol, price)
            
            # Initial grid build if empty
            if not engine.grid_levels:
                engine.build_grid(price, _portfolio)
                engine.place_grid_orders(_portfolio, exchange)
                
            # Process tick (simulates/checks fills & places replacement orders)
            events = engine.tick(price, _portfolio)
            fill_events.extend(events)
            
        except Exception as e:
            logger.warning(f"Error in grid tick for {symbol}: {e}")
            
    _portfolio.save()
    return {"status": "ok", "fills": fill_events, "portfolio": _portfolio.summary()}



def run_spot_dca_check():
    """Check for oversold DCA signals for extra buys."""
    exchange = get_spot_exchange()
    for symbol in spot_settings.asset_list:
        engine = _grid_engines.get(symbol)
        regime = engine.current_regime if engine else "RANGE"
        
        try:
            signal = _dca_manager.check(symbol, exchange, regime)
            if signal:
                mult = _dca_manager.extra_buy_multiplier(regime)
                order_size = (engine.allocated_usd * 0.05) * mult if engine else 500.0
                logger.info(f"🎯 DCA Signal Triggered [{symbol}]: {signal.trigger_type} (RSI: {signal.rsi:.1f}, mult: {mult}x) -> Buying ${order_size:.2f}")
                # Execute DCA Buy
                if engine and _portfolio.usdt_available >= order_size:
                    ticker = exchange.fetch_ticker(symbol)
                    price = ticker["last"]
                    qty = order_size / price
                    _portfolio.record_buy(symbol, qty, price, order_size, f"DCA_{int(datetime.now(timezone.utc).timestamp())}", is_dca=True)
                    _portfolio.save()
        except Exception as e:
            logger.warning(f"DCA check failed for {symbol}: {e}")


def get_spot_status() -> Dict[str, Any]:
    """Returns full JSON state for API / dashboard."""
    init_spot_engine()
    
    # Auto-build grid levels if empty
    has_empty = any(len(eng.grid_levels) == 0 for eng in _grid_engines.values())
    if has_empty:
        try:
            run_spot_grid_tick()
        except Exception as e:
            logger.warning(f"Auto grid tick in get_spot_status failed: {e}")

    regimes = {}
    grids = {}
    for sym, det in _regime_detectors.items():
        if det._cached_state:
            regimes[sym] = {
                "regime": det._cached_state.regime,
                "adx": round(det._cached_state.adx, 1),
                "plus_di": round(det._cached_state.plus_di, 1),
                "minus_di": round(det._cached_state.minus_di, 1),
                "sma_50": round(det._cached_state.sma_50, 2),
                "sma_200": round(det._cached_state.sma_200, 2),
                "price": round(det._cached_state.price, 2),
            }
            
    for sym, eng in _grid_engines.items():
        grids[sym] = eng.summary()
        
    return {
        "enabled": spot_settings.enabled,
        "paper_mode": spot_settings.paper_mode,
        "portfolio": _portfolio.summary(),
        "regimes": regimes,
        "grids": grids,
    }

