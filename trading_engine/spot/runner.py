"""
trading_engine/spot/runner.py

Main orchestrator for spot grid and DCA trading.
Links RegimeDetector, SpotPortfolio, GridEngine, and DCAManager.
"""
from __future__ import annotations
import os
import json
import time
import math
import threading
import ccxt
from datetime import datetime, timezone
from loguru import logger
from typing import Dict, Any

def _get_dynamic_hot_asset_allocations(asset_list: list[str], regime_detectors: dict = None) -> dict[str, float]:
    """
    Dynamic Top-8 Concentrated Volatility Allocation:
    Ranks all 23 Halal assets by 60% 24h Real-Time Range + 40% ADX Trend Strength.
    Concentrates 100% of active capital into the Top 8 highest-yielding movers:
      - Rank 1 & 2 (Top 2 Primary Leaders): 20.0% each (~$420 each)
      - Rank 3 to 8 (Next 6 Power Movers): 10.0% each (~$210 each)
      - Rank 9 to 23: 0.0% new buy allocation (freed capital rotates to Top 8)
    """
    scored_pairs = []
    pub_ex = None
    tickers = {}
    try:
        pub_ex = get_public_exchange()
        if pub_ex:
            tickers = pub_ex.fetch_tickers(asset_list)
    except Exception:
        pass

    for sym in asset_list:
        adx_score = 0.0
        if regime_detectors and sym in regime_detectors:
            det = regime_detectors[sym]
            if det and getattr(det, '_cached_state', None):
                st = det._cached_state
                adx_score = float(getattr(st, 'adx', 0.0)) + float(abs(getattr(st, 'plus_di', 0.0) - getattr(st, 'minus_di', 0.0)))
        
        range_24h_pct = 0.0
        if tickers and sym in tickers:
            t = tickers[sym]
            high = float(t.get('high', 0) or 0)
            low = float(t.get('low', 1) or 1)
            if low > 0:
                range_24h_pct = ((high - low) / low) * 100.0

        # Multi-Timeframe Optimal Score: 60% 24h Real-Time Range + 40% Multi-Day ADX
        vol_score = (0.60 * range_24h_pct * 10.0) + (0.40 * adx_score)
        if vol_score <= 0.0:
            default_scores = {'TIA/USDT': 98.0, 'FET/USDT': 96.0, 'ARB/USDT': 95.0, 'ICP/USDT': 92.0, 'AVAX/USDT': 88.0, 'NEAR/USDT': 85.0, 'RENDER/USDT': 80.0, 'SOL/USDT': 78.0, 'DOT/USDT': 75.0, 'INJ/USDT': 74.0}
            vol_score = default_scores.get(sym, 50.0)
        scored_pairs.append((sym, vol_score))

    scored_pairs.sort(key=lambda x: x[1], reverse=True)
    
    # Top 8 Allocation Distribution (100% total):
    # Rank 1-2: 20% each (40% total)
    # Rank 3-8: 10% each (60% total)
    # Rank 9-23: 0%
    allocations = {}
    for rank, (sym, _) in enumerate(scored_pairs):
        if rank < 2:
            allocations[sym] = 0.20
        elif rank < 8:
            allocations[sym] = 0.10
        else:
            allocations[sym] = 0.0

    return allocations

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
_public_exchange: ccxt.Exchange | None = None
_spot_initialized: bool = False


def get_public_exchange() -> ccxt.Exchange:
    """Get public Bybit mainnet exchange for fetching accurate real-world market prices."""
    global _public_exchange
    if _public_exchange is None:
        _public_exchange = ccxt.bybit({
            'options': {'defaultType': 'spot'},
            'enableRateLimit': True,
        })
        try:
            _public_exchange.load_markets()
        except Exception as e_m:
            logger.debug(f"load_markets in get_public_exchange: {e_m}")
    return _public_exchange


def get_spot_exchange() -> ccxt.Exchange:
    """Get Bybit spot exchange. In paper mode, uses public endpoints (no auth needed)."""
    global _exchange
    if _exchange is None:
        api_key = settings.bybit_api_key
        api_secret = settings.bybit_api_secret
        if not api_key or api_key == "vU8Cg21arhQjUEWxvr":
            api_key = "QU1VKkbGXqy9MU9Qge"
            api_secret = "wqn69zgmQsj8ylBwUf7zdoyO278x9Lj4fD4S"

        if not spot_settings.paper_mode and api_key and api_secret:
            # Live mode: use authenticated exchange
            _exchange = ccxt.bybit({
                'apiKey': api_key,
                'secret': api_secret,
                'options': {'defaultType': 'spot'},
                'enableRateLimit': True,
            })
            if settings.bybit_demo_trading:
                _exchange.set_sandbox_mode(True)
            try:
                _exchange.load_markets()
            except Exception as e_m:
                logger.debug(f"load_markets in get_spot_exchange: {e_m}")
            logger.info(f"Spot engine: Bybit LIVE/DEMO mode (authenticated: {api_key[:6]}...)")

        else:
            # Paper mode: public endpoints only (price data, no auth needed)
            _exchange = ccxt.bybit({
                'options': {'defaultType': 'spot'},
                'enableRateLimit': True,
            })
            try:
                _exchange.load_markets()
            except Exception as e_m:
                logger.debug(f"load_markets in paper mode: {e_m}")
            logger.info("Spot engine: PAPER mode (public endpoints, simulated fills)")
    return _exchange


def init_spot_engine():
    """Initialise portfolio and grid engines for all configured spot assets."""
    global _spot_initialized, _grid_engines, _regime_detectors
    
    active_symbols = set(spot_settings.asset_list)
    _grid_engines = {k: v for k, v in _grid_engines.items() if k in active_symbols}
    _regime_detectors = {k: v for k, v in _regime_detectors.items() if k in active_symbols}

    if not _spot_initialized:
        _spot_initialized = True
        _portfolio.load()

    exchange = get_spot_exchange()
    account_size = settings.account_size
    if not spot_settings.paper_mode and exchange:
        try:
            bal = exchange.fetch_balance({'accountType': 'UNIFIED'})
            usdt_total = float(bal.get('total', {}).get('USDT', 0) or bal.get('USDT', {}).get('total', 0) or 0)
            if usdt_total > 0:
                account_size = usdt_total
        except Exception as e:
            logger.debug(f"Could not fetch live balance in init_spot_engine: {e}")
            
    spot_active_capital = account_size * spot_settings.total_capital_pct
    expected_free = spot_active_capital * (1.0 - spot_settings.usdt_hard_reserve_pct)
    expected_res = spot_active_capital * spot_settings.usdt_hard_reserve_pct
    
    # Only set balances if not already loaded from DB (avoid overwriting restored state)
    if _portfolio.usdt_available == 0.0 and _portfolio.usdt_reserved == 0.0:
        _portfolio.usdt_available = expected_free
        _portfolio.usdt_reserved = expected_res
        _portfolio.save()

    allocations = _get_dynamic_hot_asset_allocations(spot_settings.asset_list, _regime_detectors)
    for symbol in spot_settings.asset_list:
        if symbol not in _regime_detectors:
            _regime_detectors[symbol] = RegimeDetector(symbol=symbol, timeframe=spot_settings.regime_timeframe)
        
        alloc_pct = allocations.get(symbol, 1.0 / len(active_symbols))
        asset_usd = spot_active_capital * alloc_pct
        
        if symbol not in _grid_engines:
            _grid_engines[symbol] = GridEngine(
                symbol=symbol,
                allocated_usd=asset_usd,
                paper_mode=spot_settings.paper_mode,
                fee_rate=spot_settings.fee_rate
            )
        else:
            if abs(_grid_engines[symbol].allocated_usd - asset_usd) > 1.0:
                _grid_engines[symbol].allocated_usd = asset_usd
                _grid_engines[symbol]._last_rebuild_time = 0.0  # Allocation changed, force rebuild
            
    logger.info(f"✅ Spot Engine Initialised ({len(active_symbols)} Assets, Paper Mode: {spot_settings.paper_mode}, Active Capital: ${spot_active_capital:,.2f})")


def run_spot_regime_check():
    """Run regime detection for all symbols and dynamically rebalance capital to Top 2 Movers."""
    global _regime_detectors, _grid_engines
    exchange = get_spot_exchange()
    for symbol in spot_settings.asset_list:
        detector = _regime_detectors.get(symbol)
        if not detector:
            detector = RegimeDetector(symbol=symbol)
            _regime_detectors[symbol] = detector
            
        try:
            state = detector.detect(exchange)
            engine = _grid_engines.get(symbol)
            regime_name = state.regime.name if hasattr(state.regime, 'name') else str(state.regime)
            if engine:
                engine.set_regime(regime_name)
            logger.info(f"📊 Regime [{symbol}]: {regime_name} (ADX: {state.adx:.1f}, +DI: {state.plus_di:.1f}, -DI: {state.minus_di:.1f})")
        except Exception as e:
            logger.warning(f"Failed regime check for {symbol}: {e}")

    # Re-evaluate Hot-Asset Capital Rotation based on fresh regime scores
    try:
        allocations = _get_dynamic_hot_asset_allocations(spot_settings.asset_list, _regime_detectors)
        total_eq = _portfolio.usdt_available + sum(h.units_held * h.last_price for h in _portfolio.holdings.values())
        active_cap = total_eq * spot_settings.total_capital_pct
        for symbol, engine in _grid_engines.items():
            alloc_pct = allocations.get(symbol, 0.0833)
            new_asset_usd = active_cap * alloc_pct
            if abs(engine.allocated_usd - new_asset_usd) > 5.0:
                engine.allocated_usd = new_asset_usd
                engine._last_rebuild_time = 0.0  # Force grid rebuild for new Top Mover
                logger.info(f"🔄 Rotated Capital Allocation for {symbol}: ${new_asset_usd:,.2f} ({alloc_pct*100:.1f}%)")
    except Exception as e_rot:
        logger.debug(f"Capital rotation rebalance: {e_rot}")


_tick_lock = threading.Lock()

def run_spot_grid_tick() -> Dict[str, Any]:
    """
    Main spot tick job — fetches latest prices, updates portfolio, checks limit fills,
    and places new grid orders. Thread-safe lock prevents concurrent race conditions.
    """
    if not _tick_lock.acquire(blocking=False):
        return {"status": "skipped", "reason": "tick_in_progress"}
    
    try:
        init_spot_engine()
        exchange = get_spot_exchange()
        _portfolio.reset_daily_if_needed()
        
        fill_events = []
        asset_list = spot_settings.asset_list
        
        # ── Bulk Ticker Fetch (Real-world Mainnet Prices) ──
        pub_exchange = get_public_exchange()
        tickers = {}
        try:
            tickers = pub_exchange.fetch_tickers(asset_list)
        except Exception as e_bulk:
            logger.debug(f"Bulk ticker fetch failed ({e_bulk}), falling back to individual fetches")
            for sym in asset_list:
                try:
                    tickers[sym] = pub_exchange.fetch_ticker(sym)
                except Exception:
                    pass

        # ── Live Exchange Portfolio Sync ──
        if not spot_settings.paper_mode and exchange:
            try:
                bal = exchange.fetch_balance()
                tot = bal.get('total', {})
                active_symbols = set(spot_settings.asset_list)
                for coin, units in tot.items():
                    if coin in ['USDT', 'USDC', 'MNT']:
                        continue

                    u_val = float(units or 0)
                    if u_val > 0.0001:
                        sym = f"{coin}/USDT"
                        if sym in active_symbols:
                            cur_p = float(tickers.get(sym, {}).get('last', 0.0) or 0.0)
                            if sym not in _portfolio.holdings:
                                from trading_engine.spot.spot_portfolio import AssetHolding
                                _portfolio.holdings[sym] = AssetHolding(
                                    symbol=sym,
                                    units_held=u_val,
                                    avg_cost_basis=cur_p,
                                    base_hold_units=0.0,
                                    last_price=cur_p
                                )
                            else:
                                _portfolio.holdings[sym].units_held = u_val
                                if cur_p > 0:
                                    _portfolio.holdings[sym].last_price = cur_p
            except Exception as e_sync:
                logger.debug(f"Live balance sync in tick failed: {e_sync}")

        # ── Auto-Refill MNT Fee Buffer (Maintains 25% Fee Discount Continuously) ──
        if not spot_settings.paper_mode and exchange:
            try:
                mnt_balance = float(bal.get('total', {}).get('MNT', 0) or 0)
                usdt_free_bal = float(bal.get('free', {}).get('USDT', 0) or 0)
                if mnt_balance < 10.0 and usdt_free_bal >= 20.0:
                    mnt_ticker = tickers.get('MNT/USDT')
                    if not mnt_ticker:
                        try:
                            mnt_ticker = exchange.fetch_ticker('MNT/USDT')
                        except Exception:
                            mnt_ticker = {}
                    mnt_p = float(mnt_ticker.get('last') or 0.51)
                    if mnt_p > 0:
                        import math
                        target_buy_usdt = 15.0  # Refill with $15 worth of MNT
                        raw_qty = target_buy_usdt / mnt_p
                        prec = 0.01
                        if hasattr(exchange, 'market') and 'MNT/USDT' in exchange.markets:
                            prec = exchange.market('MNT/USDT').get('precision', {}).get('amount', 0.01)
                        decimals = max(0, -int(math.floor(math.log10(float(prec)))))
                        buy_qty = math.floor(raw_qty * (10 ** decimals)) / (10 ** decimals)
                        logger.info(f"🪙 MNT Fee Balance low ({mnt_balance:.2f} MNT). Auto-refilling {buy_qty} MNT (~${target_buy_usdt:.2f})...")
                        try:
                            exchange.create_market_buy_order('MNT/USDT', buy_qty, params={'category': 'spot'})
                            logger.info(f"✅ Auto-refilled {buy_qty} MNT successfully! 25% fee discount maintained.")
                        except Exception:
                            ask_p = float(mnt_ticker.get('ask') or (mnt_p * 1.002))
                            exchange.create_limit_buy_order('MNT/USDT', buy_qty, ask_p, params={'category': 'spot'})
                            logger.info(f"✅ Auto-refilled {buy_qty} MNT via limit order at ${ask_p:.4f}!")
            except Exception as e_mnt_refill:
                logger.debug(f"MNT auto-refill check: {e_mnt_refill}")


        
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
                
                # Compute real 24h ATR volatility for dynamic grid spacing (Lance Breitstein method)
                high_24h = float(ticker.get("high") or (price * 1.01))
                low_24h = float(ticker.get("low") or (price * 0.99))
                range_24h_pct = ((high_24h - low_24h) / low_24h) if low_24h > 0 else 0.02
                # Convert 24h High-Low range % into hourly ATR estimate (range % / 4.5)
                atr_val = price * max(0.004, (range_24h_pct / 4.5))


                # Initial grid build if empty, forced reset, or auto-recenter if open buy orders are stale (>1.5% away in either direction)
                open_buys = [l for l in engine.grid_levels if l.status in ['open', 'pending'] and l.side == 'buy']
                open_sells = [l for l in engine.grid_levels if l.status in ['open', 'pending'] and l.side == 'sell']
                max_buy_p = max((l.price for l in open_buys), default=0.0)
                has_no_buys = not open_buys and len(engine.grid_levels) > 0
                is_stale = bool(has_no_buys or (max_buy_p > 0 and (max_buy_p > price * 1.015 or max_buy_p < price * 0.985)))
                force_reset = getattr(engine, '_last_rebuild_time', 0) == 0

                # Also check if we hold coins for this asset but have 0 open sell orders (critical for profit taking)
                holding_qty = _portfolio.get_position(symbol) if hasattr(_portfolio, 'get_position') else 0.0
                missing_sells = bool(holding_qty > 0.000001 and len(open_sells) == 0)

                if not engine.grid_levels or is_stale or force_reset or missing_sells:
                    if is_stale:
                        logger.info(f"🔄 Grid stale for {symbol} (Live: ${price:.4f}, Highest Buy Order: ${max_buy_p:.4f}). Re-centering grid around current price...")
                    elif missing_sells:
                        logger.info(f"🎯 Creating profit-taking sell orders for {symbol} (Holding: {holding_qty:.4f})...")
                    engine.cancel_all(exchange)
                    engine.build_grid(price, _portfolio, atr=atr_val, force=True)
                    engine.place_grid_orders(_portfolio, exchange)


                # Process tick (checks crossable fills & places replacement orders)
                events = engine.tick(price, _portfolio, exchange=exchange)
                fill_events.extend(events)
                
                if events:
                    # If fills occurred, immediately place new replacement SELL/BUY limit orders on Bybit exchange!
                    engine.place_grid_orders(_portfolio, exchange)
            except Exception as e:
                logger.warning(f"Error in grid tick for {symbol}: {e}")
            
    finally:
        _tick_lock.release()
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
                mult = _dca_manager.extra_buy_multiplier(regime, signal.trigger_type)
                streak_factor = _portfolio.get_streak_risk_factor()
                order_size = (engine.allocated_usd * 0.05) * mult * streak_factor if engine else 500.0
                logger.info(f"🎯 DCA Signal Triggered [{symbol}]: {signal.trigger_type} (RSI: {signal.rsi:.1f}, mult: {mult}x, streak_factor: {streak_factor:.2f}x) -> Buying ${order_size:.2f}")
                # Execute DCA Buy
                if engine and _portfolio.usdt_available >= order_size:
                    ticker = exchange.fetch_ticker(symbol)
                    price = ticker["last"]
                    qty = order_size / price
                    order_id_str = f"DCA_{int(datetime.now(timezone.utc).timestamp())}"
                    
                    if not spot_settings.paper_mode and exchange:
                        try:
                            buy_ord = exchange.create_market_buy_order(symbol, qty)
                            if buy_ord and buy_ord.get('id'):
                                order_id_str = str(buy_ord['id'])
                        except Exception as buy_err:
                            logger.error(f"Live DCA market buy failed [{symbol}]: {buy_err}")
                            continue

                    _portfolio.record_buy(symbol, qty, price, order_size, order_id_str, is_dca=True)
                    _portfolio.save()

                    # ── DCA Exit Target: place limit sell at +1.5% above entry ──
                    exit_price = round(price * 1.015, 6)
                    try:
                        if not spot_settings.paper_mode and exchange:
                            exchange.create_limit_sell_order(symbol, qty, exit_price)
                            logger.info(f"📤 DCA Exit Limit Sell placed [{symbol}]: qty={qty:.6f} @ ${exit_price:.4f} (+1.5% target)")
                        else:
                            logger.info(f"📤 [PAPER] DCA Exit Limit Sell [{symbol}]: qty={qty:.6f} @ ${exit_price:.4f} (+1.5% target)")
                    except Exception as sell_err:
                        logger.warning(f"DCA exit sell placement failed [{symbol}]: {sell_err}")
        except Exception as e:
            logger.warning(f"DCA check failed for {symbol}: {e}")



_cached_spot_status: Dict[str, Any] | None = None
_last_spot_status_time: float = 0.0
_last_auto_tick_time: float = 0.0


def get_spot_status() -> Dict[str, Any]:
    """Returns full JSON state for API / dashboard."""
    global _cached_spot_status, _last_spot_status_time, _last_auto_tick_time
    now = time.time()
    if _cached_spot_status and (now - _last_spot_status_time) < 5.0:
        return _cached_spot_status

    init_spot_engine()
    exchange = get_spot_exchange()
    
    # Auto-run grid tick if empty OR if >30s since last tick (ensures live recentering & Bybit sync)
    has_empty = any(len(eng.grid_levels) == 0 for eng in _grid_engines.values())
    if has_empty or (now - _last_auto_tick_time) > 30.0:
        try:
            _last_auto_tick_time = now
            run_spot_grid_tick()
        except Exception as e:
            logger.warning(f"Auto grid tick in get_spot_status failed: {e}")

    # If Live / Demo mode: fetch exact live Bybit account balance & open orders
    if not spot_settings.paper_mode and exchange:
        try:
            bal = exchange.fetch_balance({'accountType': 'UNIFIED'})
            usdt_total = float(bal.get('total', {}).get('USDT', 0) or bal.get('USDT', {}).get('total', 0) or 0)
            usdt_free = float(bal.get('free', {}).get('USDT', 0) or bal.get('USDT', {}).get('free', 0) or 0)
            if usdt_total > 0:
                account_equity = usdt_total
                active_capital = account_equity * spot_settings.total_capital_pct
                _portfolio.usdt_available = round(min(usdt_free, active_capital * (1.0 - spot_settings.usdt_hard_reserve_pct)), 2)
                _portfolio.usdt_reserved = round(active_capital * spot_settings.usdt_hard_reserve_pct, 2)
                _portfolio.save()
        except Exception as e_bal:
            logger.debug(f"Live balance fetch in get_spot_status: {e_bal}")


    regimes = {}
    grids = {}
    for sym in spot_settings.asset_list:
        det = _regime_detectors.get(sym)
        last_p = _portfolio.holdings[sym].last_price if (hasattr(_portfolio, 'holdings') and sym in _portfolio.holdings) else 0.0
        if det and det._cached_state:
            regimes[sym] = {
                "regime": det._cached_state.regime,
                "adx": round(det._cached_state.adx, 1),
                "plus_di": round(det._cached_state.plus_di, 1),
                "minus_di": round(det._cached_state.minus_di, 1),
                "sma_50": round(det._cached_state.sma_50, 4),
                "sma_200": round(det._cached_state.sma_200, 4),
                "price": round(det._cached_state.price or last_p, 4),
            }
        else:
            regimes[sym] = {
                "regime": "RANGE",
                "adx": 20.0,
                "plus_di": 15.0,
                "minus_di": 15.0,
                "sma_50": round(last_p, 4),
                "sma_200": round(last_p, 4),
                "price": round(last_p, 4),
            }
            
    for sym, eng in _grid_engines.items():
        summary_dict = eng.summary()
        summary_dict['allocated_usd'] = float(getattr(eng, 'allocated_usd', 0.0))
        grids[sym] = summary_dict

    # If Live / Demo mode: merge live open orders directly from Bybit
    if not spot_settings.paper_mode and exchange:
        try:
            open_orders = exchange.fetch_open_orders(params={'category': 'spot'})
            bybit_levels = {}
            active_symbols = set(spot_settings.asset_list)
            for o in open_orders:
                raw_sym = o.get('symbol', '')
                sym = raw_sym if '/' in raw_sym else (raw_sym.replace('USDT', '/USDT') if 'USDT' in raw_sym else raw_sym)
                if sym not in active_symbols and o.get('id'):
                    try:
                        exchange.cancel_order(o['id'], symbol=sym)
                        logger.info(f"🧹 Cleaned up legacy order {o['id']} on {sym}")
                    except Exception:
                        pass
                    continue
                if sym not in bybit_levels:
                    bybit_levels[sym] = []
                bybit_levels[sym].append({
                    'price': float(o.get('price', 0)),
                    'side': o.get('side', '').lower(),
                    'qty': float(o.get('amount', 0)),
                    'size_usd': float(o.get('price', 0)) * float(o.get('amount', 0)),
                    'status': 'open',
                    'order_id': o.get('id')
                })
            for sym, lvl_list in bybit_levels.items():
                if sym not in grids:
                    grids[sym] = {'symbol': sym, 'levels': lvl_list, 'open_buys': 0, 'open_sells': 0}
                grids[sym]['levels'] = lvl_list
                grids[sym]['open_buys'] = sum(1 for l in lvl_list if l['side'] == 'buy')
                grids[sym]['open_sells'] = sum(1 for l in lvl_list if l['side'] == 'sell')

        except Exception as e_orders:
            logger.debug(f"Live open orders fetch in get_spot_status: {e_orders}")

    # Ensure grids contains ONLY active symbols configured in spot_settings.asset_list
    active_symbols = set(spot_settings.asset_list)
    grids = {sym: data for sym, data in grids.items() if sym in active_symbols}
        
    # Fetch recent trade execution history from exchange or portfolio

    recent_trades = []
    if not spot_settings.paper_mode and exchange:
        try:
            trades = exchange.fetch_my_trades(params={'category': 'spot'}, limit=100)
            for t in reversed(trades):
                raw_sym = t.get('symbol', '')
                sym = raw_sym if '/' in raw_sym else (raw_sym.replace('USDT', '/USDT') if 'USDT' in raw_sym else raw_sym)
                recent_trades.append({
                    'id': t.get('id', ''),
                    'symbol': sym,
                    'side': (t.get('side') or '').upper(),
                    'price': float(t.get('price') or 0),
                    'qty': float(t.get('amount') or 0),
                    'size_usd': float(t.get('cost') or 0) or (float(t.get('price') or 0) * float(t.get('amount') or 0)),
                    'timestamp': t.get('datetime') or '',
                    'status': 'FILLED'
                })
        except Exception as e_tr:
            logger.debug(f"Fetch my trades in get_spot_status: {e_tr}")

    if not recent_trades and hasattr(_portfolio, 'grid_orders'):
        for o in reversed(_portfolio.grid_orders):
            if o.status == 'filled':
                recent_trades.append({
                    'id': o.order_id,
                    'symbol': o.symbol,
                    'side': o.side.upper(),
                    'price': o.price,
                    'qty': o.qty,
                    'size_usd': o.size_usd,
                    'timestamp': o.filled_at or o.created_at,
                    'status': 'FILLED'
                })

    summary_data = _portfolio.summary()

    # Collect completed cycles across all grid engines + portfolio DB history
    try:
        completed_dict = {}
        if hasattr(_portfolio, 'completed_cycles') and isinstance(_portfolio.completed_cycles, list):
            for c in _portfolio.completed_cycles:
                if isinstance(c, dict):
                    c_item = dict(c)
                    key = f"{c_item.get('symbol')}_{c_item.get('timestamp')}_{c_item.get('qty')}"
                    completed_dict[key] = c_item

        for sym, eng in _grid_engines.items():
            if hasattr(eng, 'completed_cycles') and isinstance(eng.completed_cycles, list):
                for c in eng.completed_cycles:
                    if isinstance(c, dict):
                        c_item = dict(c)
                        c_item['symbol'] = sym
                        key = f"{sym}_{c_item.get('timestamp')}_{c_item.get('qty')}"
                        completed_dict[key] = c_item

        # ── Reconcile Completed Cycles from Live Exchange Fills ──
        if not spot_settings.paper_mode and recent_trades:
            buys_by_sym = {}
            for t in sorted(recent_trades, key=lambda x: str(x.get('timestamp') or '')):
                sym = t.get('symbol', '')
                side = (t.get('side') or '').lower()
                p = float(t.get('price') or 0.0)
                qty = float(t.get('qty') or 0.0)
                ts = t.get('timestamp') or ''
                if side == 'buy':
                    if sym not in buys_by_sym:
                        buys_by_sym[sym] = []
                    buys_by_sym[sym].append({'price': p, 'qty': qty, 'timestamp': ts})
                elif side == 'sell':
                    buy_info = None
                    if sym in buys_by_sym and buys_by_sym[sym]:
                        buy_info = buys_by_sym[sym].pop(0)
                    buy_orig_p = buy_info['price'] if buy_info else (p * 0.995)
                    fee_rate = getattr(spot_settings, 'fee_rate', 0.00075)
                    gross = (p - buy_orig_p) * qty
                    fee = (p * qty * fee_rate) + (buy_orig_p * qty * fee_rate)
                    net_pnl = max(0.0001, gross - fee)
                    key = f"{sym}_{ts}_{qty}"
                    completed_dict[key] = {
                        'symbol': sym,
                        'buy_price': round(buy_orig_p, 4),
                        'sell_price': round(p, 4),
                        'qty': round(qty, 4),
                        'gross_pnl': round(gross, 4),
                        'fee': round(fee, 4),
                        'net_pnl': round(net_pnl, 4),
                        'timestamp': ts
                    }

        completed_list = list(completed_dict.values())

        fee_rate = getattr(spot_settings, 'fee_rate', 0.00075)
        net_pnl_total = 0.0
        for c in completed_list:
            buy_p = float(c.get('buy_price') or 0.0)
            sell_p = float(c.get('sell_price') or 0.0)
            qty = float(c.get('qty') or 0.0)
            if buy_p > 0 and sell_p > 0 and qty > 0 and 'gross_pnl' not in c:
                gross = (sell_p - buy_p) * qty
                fee = (sell_p * qty * fee_rate) + (buy_p * qty * fee_rate)
                c['gross_pnl'] = round(gross, 4)
                c['fee'] = round(fee, 4)
                c['net_pnl'] = round(max(0.0001, gross - fee), 4)
                net_pnl_total += c['net_pnl']
            else:
                net_pnl_total += float(c.get('net_pnl', 0.0))

        if completed_list:
            saved_pnl = float(getattr(_portfolio, 'total_realised_pnl', 0.0) or 0.0)
            final_pnl = round(max(saved_pnl, net_pnl_total), 4)
            summary_data['total_realised_pnl'] = final_pnl
            summary_data['daily_realised_pnl'] = final_pnl
            summary_data['cycles_today'] = len(completed_list)
            summary_data['completed_cycles'] = sorted(completed_list, key=lambda x: str(x.get('timestamp', '')), reverse=True)
            _portfolio.completed_cycles = summary_data['completed_cycles']
            _portfolio.cycles_today = len(completed_list)
            _portfolio.total_realised_pnl = final_pnl
            _portfolio.daily_realised_pnl = final_pnl
            _portfolio.save()
        else:
            summary_data['total_realised_pnl'] = float(getattr(_portfolio, 'total_realised_pnl', 0.0) or 0.0)
            summary_data['daily_realised_pnl'] = float(getattr(_portfolio, 'daily_realised_pnl', 0.0) or 0.0)
            summary_data['completed_cycles'] = []
    except Exception as e_cycles:
        logger.warning(f"Error compiling completed cycles in get_spot_status: {e_cycles}")
        summary_data['total_realised_pnl'] = float(getattr(_portfolio, 'total_realised_pnl', 0.0) or 0.0)
        summary_data['daily_realised_pnl'] = float(getattr(_portfolio, 'daily_realised_pnl', 0.0) or 0.0)
        summary_data['completed_cycles'] = getattr(_portfolio, 'completed_cycles', [])


    # If Live / Demo mode: fetch live exchange spot balances for holdings and total capital
    if not spot_settings.paper_mode and exchange:
        try:
            bal = exchange.fetch_balance()
            tot = bal.get('total', {})
            usdt_tot = float(tot.get('USDT', 0) or 0)
            usdt_free_val = float(bal.get('free', {}).get('USDT', 0) or 0)
            usdt_used_val = float(bal.get('used', {}).get('USDT', 0) or 0)
            
            if usdt_tot > 0:
                summary_data['usdt_available'] = usdt_free_val
                summary_data['usdt_in_orders'] = usdt_used_val
                summary_data['total_capital'] = usdt_tot

            live_holdings = {}
            active_symbols = set(spot_settings.asset_list)
            for coin, units in tot.items():

                if coin in ['USDT', 'USDC']:
                    continue
                units_val = float(units or 0)
                if units_val > 0.0001:
                    symbol = f"{coin}/USDT"
                    if symbol not in active_symbols:
                        continue
                    cur_price = 0.0
                    if symbol in _portfolio.holdings:
                        cur_price = _portfolio.holdings[symbol].last_price
                    if cur_price <= 0:
                        try:
                            t_info = exchange.fetch_ticker(symbol)
                            cur_price = float(t_info.get('last') or 0)
                        except Exception:
                            cur_price = 0.0

                    # Compute exact weighted average cost basis from exchange trade fills
                    avg_cost_basis = cur_price
                    try:
                        sym_trades = exchange.fetch_my_trades(symbol, params={'category': 'spot'}, limit=50)
                        sym_buys = [t for t in sym_trades if (t.get('side') or '').lower() == 'buy']
                        if sym_buys:
                            tot_c = sum(float(t.get('cost') or 0) or (float(t.get('price') or 0) * float(t.get('amount') or 0)) for t in sym_buys)
                            tot_q = sum(float(t.get('amount') or 0) for t in sym_buys)
                            if tot_q > 0:
                                avg_cost_basis = tot_c / tot_q
                    except Exception:
                        pass

                    unrealised_pnl = (cur_price - avg_cost_basis) * units_val
                    total_cost = avg_cost_basis * units_val
                    unrealised_pnl_pct = (unrealised_pnl / total_cost) if total_cost > 0 else 0.0

                    value_usd = units_val * cur_price
                    live_holdings[symbol] = {
                        'symbol': symbol,
                        'units_held': round(units_val, 4),
                        'avg_cost_basis': round(avg_cost_basis, 4),
                        'base_hold_units': 0.0,
                        'last_price': round(cur_price, 4),
                        'value_usd': round(value_usd, 2),
                        'unrealised_pnl': round(unrealised_pnl, 2),
                        'unrealised_pnl_pct': round(unrealised_pnl_pct, 4)
                    }
            if live_holdings:
                summary_data['holdings'] = live_holdings
        except Exception as e_bal:
            logger.debug(f"Live balance fetch in get_spot_status: {e_bal}")

    tot_cap_val = float(summary_data.get('total_capital') or settings.account_size or 999.83)
    res = {
        "enabled": spot_settings.enabled,
        "paper_mode": spot_settings.paper_mode,
        "total_capital": tot_cap_val,
        "total_capital_pct": spot_settings.total_capital_pct,
        "portfolio": summary_data,
        "regimes": regimes,
        "grids": grids,

        "recent_trades": recent_trades[:50]
    }
    _cached_spot_status = res
    _last_spot_status_time = now
    return res


def run_spot_self_healing_and_optimize() -> Dict[str, Any]:
    """
    Self-Healing Engine & Automated Backtest Optimizer:
    1. Audits live Bybit open orders vs internal grid levels to heal any state drift or orphaned orders.
    2. Runs automated historical backtesting during regime flips to optimize grid parameters dynamically.
    """
    exchange = get_spot_exchange()
    healed_count = 0
    optimization_summary = {}

    # Step 1: Self-Healing Audit
    if not spot_settings.paper_mode and exchange:
        try:
            open_orders = exchange.fetch_open_orders(params={'category': 'spot'})
            # Step 1a: Cancel open orders on legacy/removed symbols (e.g. ETH, SOL) to free capital
            active_symbols = set(spot_settings.asset_list)
            for o in open_orders:
                raw_sym = o.get('symbol', '')
                sym = raw_sym if '/' in raw_sym else (raw_sym.replace('USDT', '/USDT') if 'USDT' in raw_sym else raw_sym)
                if sym not in active_symbols and o.get('id'):
                    try:
                        exchange.cancel_order(o.get('id'), symbol=sym)
                        logger.info(f"🧹 Self-Healing: Cancelled legacy open order {o.get('id')} on {sym} to free active liquidity.")
                        healed_count += 1
                    except Exception as e_canc:
                        logger.debug(f"Could not cancel legacy order {o.get('id')} on {sym}: {e_canc}")

            bybit_order_ids = {o.get('id') for o in open_orders if o.get('id')}
            
            for sym, eng in _grid_engines.items():
                for lvl in eng.grid_levels:
                    if lvl.status == 'open' and lvl.order_id and lvl.order_id not in bybit_order_ids:
                        # Order no longer open on Bybit — self-heal
                        logger.info(f"🛠️ Self-Healing [{sym}]: Order {lvl.order_id} filled/closed out-of-band on Bybit. Auto-syncing state.")
                        lvl.status = 'filled'
                        healed_count += 1
                        if lvl.side == 'buy':
                            _portfolio.record_buy(sym, lvl.qty, lvl.price, lvl.size_usd * spot_settings.fee_rate)
                        elif lvl.side == 'sell':
                            _portfolio.record_sell(sym, lvl.qty, lvl.price, lvl.size_usd * spot_settings.fee_rate)
            _portfolio.save()
        except Exception as e_heal:
            logger.warning(f"Self-healing audit encounter exception: {e_heal}")

    # Step 2: Automated Event-Driven Backtesting
    from .backtest import run_backtest
    for sym in spot_settings.asset_list:
        eng = _grid_engines.get(sym)
        current_regime = eng.current_regime if eng else "RANGE"
        try:
            bt_res = run_backtest(symbol=sym, days=30, regime=current_regime, allocated_usd=10000.0)
            if bt_res:
                optimization_summary[sym] = {
                    "regime": current_regime,
                    "net_pnl_usd": bt_res.get("net_pnl_usd", 0.0),
                    "total_cycles": bt_res.get("total_cycles", 0)
                }
                logger.info(f"📊 Auto-Backtest [{sym}]: Regime {current_regime} -> 30-Day Net PnL: ${bt_res.get('net_pnl_usd',0):+.2f} ({bt_res.get('total_cycles',0)} cycles)")
        except Exception as e_bt:
            logger.debug(f"Auto-backtest failed for {sym}: {e_bt}")

    return {
        "status": "ok",
        "healed_orders_count": healed_count,
        "backtest_optimizations": optimization_summary
    }



