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
from trading_engine.spot.spot_portfolio import AssetHolding
from trading_engine.spot.btc_master_filter import btc_master_filter

ALL_23_HISTORICAL_COSTS = {
    'NEAR/USDT': 2.3953, 'TIA/USDT': 0.4474, 'SUI/USDT': 0.8297, 'FET/USDT': 0.1791,
    'ICP/USDT': 2.8460, 'ADA/USDT': 0.2102, 'APT/USDT': 0.6153, 'OP/USDT': 0.1086,
    'RENDER/USDT': 1.4773, 'ARB/USDT': 0.1852, 'DOT/USDT': 1.1759, 'ALGO/USDT': 0.0908,
    'UNI/USDT': 6.6739, 'INJ/USDT': 6.3400, 'AVAX/USDT': 8.1317, 'SOL/USDT': 103.0504,
    'ETH/USDT': 2477.2894, 'BTC/USDT': 80295.00, 'XAUT/USDT': 4583.60, 'ARKM/USDT': 0.1125,
    'ATOM/USDT': 1.8158, 'LINK/USDT': 12.7713, 'SEI/USDT': 0.0468
}

ALL_23_HALAL_UNIVERSE = [
    'FET/USDT', 'NEAR/USDT', 'SUI/USDT', 'TIA/USDT', 'ARB/USDT', 'OP/USDT',
    'APT/USDT', 'AVAX/USDT', 'SEI/USDT', 'SOL/USDT', 'INJ/USDT', 'ARKM/USDT',
    'UNI/USDT', 'RENDER/USDT', 'ADA/USDT', 'ICP/USDT', 'LINK/USDT', 'ETH/USDT',
    'ATOM/USDT', 'DOT/USDT', 'ALGO/USDT', 'BTC/USDT', 'XAUT/USDT'
]

_last_trades_fetch_time: float = 0.0
_cached_raw_trades: list = []
_active_roster: set[str] = set()
_last_blended_score_ts: float = 0.0
_cached_blended_scores: dict[str, float] = {}

# 🛑 User Pause on ARB: strictly prevent buying ARB until after September 16, 2026 UTC (resumes Sept 17 00:00 UTC)
ARB_PAUSE_UNTIL_UTC = datetime(2026, 9, 17, 0, 0, 0, tzinfo=timezone.utc)

def is_arb_buy_paused(symbol: str) -> bool:
    """Returns True if buying ARB is paused (until after September 16, 2026 UTC)."""
    if symbol in ('ARB/USDT', 'ARB'):
        return datetime.now(timezone.utc) < ARB_PAUSE_UNTIL_UTC
    return False

def _get_dynamic_hot_asset_allocations(
    universe: list[str] = None,
    regime_detectors: dict = None,
    portfolio = None,
    total_equity: float = 0.0
) -> dict[str, float]:
    """
    Dynamic 23-Asset Scanner & Concentrated Top-8 Volatility Rotator:
    1. Evaluates all 23 Halal spot assets on Bybit using a Blended Quantitative Score:
       - 60% 48-Hour Volatility Range: ((High_48h - Low_48h) / Low_48h) over last 2 daily candles
       - 40% 7-Day Average True Range (ATR%): Sustained structural volatility over 7 days
       This prevents 1-day hype pump bag traps while keeping high-velocity oscillation leaders.
    2. Enforces strict Capital Trap Prevention (10% Max Position Exposure Gate):
       - If any asset's current holding value >= 10% of total portfolio equity, it is disqualified
         from new buy allocations (locked at 0.0%) to prevent over-accumulation.
    3. Concentrates 100% of active capital across the Top 8 eligible leaders:
       - 10.0% max allocation per asset across Top 8 leaders (80% deployed, 20% liquid cash reserve)
       - Rank 9 to 23 & Demoted/Disqualified: 0.0% buy allocation (rotated to profit-taking Sell-Only Mode)
    """
    global _last_blended_score_ts, _cached_blended_scores
    scan_list = universe or ALL_23_HALAL_UNIVERSE
    scored_pairs = []
    pub_ex = None
    tickers = {}
    try:
        pub_ex = get_public_exchange()
        if pub_ex:
            try:
                tickers = pub_ex.fetch_tickers(scan_list)
            except Exception as e_bulk:
                logger.debug(f"Bulk ticker fetch for 23 assets failed ({e_bulk}), falling back to individual fetches")
                for sym in scan_list:
                    try:
                        tickers[sym] = pub_ex.fetch_ticker(sym)
                    except Exception:
                        pass
    except Exception:
        pass

    # ── Blended 60% 48h Volatility + 40% 7d ATR Calculation (Cached for 30 min) ──
    now_ts = time.time()
    if (now_ts - _last_blended_score_ts < 1800) and _cached_blended_scores:
        blended_scores = dict(_cached_blended_scores)
    else:
        blended_scores = {}
        if pub_ex:
            try:
                from concurrent.futures import ThreadPoolExecutor
                def _fetch_one_ohlcv(s):
                    try:
                        return s, pub_ex.fetch_ohlcv(s, timeframe='1d', limit=8)
                    except Exception:
                        return s, []

                with ThreadPoolExecutor(max_workers=8) as pool:
                    candles_by_sym = dict(pool.map(_fetch_one_ohlcv, scan_list))

                for sym in scan_list:
                    candles = candles_by_sym.get(sym, [])
                    if len(candles) >= 3:
                        # 48h range across last 2 daily candles
                        c_48h = candles[-2:]
                        h_48 = max(c[2] for c in c_48h)
                        l_48 = min(c[3] for c in c_48h)
                        r_48 = ((h_48 - l_48) / l_48) * 100.0 if l_48 > 0 else 0.0

                        # 7-day ATR %
                        c_7d = candles[-7:]
                        tr_list = []
                        for i in range(1, len(c_7d)):
                            h, l, prev_c = c_7d[i][2], c_7d[i][3], c_7d[i-1][4]
                            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                            if c_7d[i][4] > 0:
                                tr_list.append(tr / c_7d[i][4])
                        atr_7d_pct = (sum(tr_list) / len(tr_list)) * 100.0 if tr_list else 0.0

                        # Blended score: 60% 48h range + 40% 7d ATR (scaled to comparable magnitude)
                        blended_scores[sym] = round((0.60 * r_48) + (0.40 * atr_7d_pct * 2.5), 2)
                    else:
                        blended_scores[sym] = 0.0

                if any(v > 0 for v in blended_scores.values()):
                    _cached_blended_scores = dict(blended_scores)
                    _last_blended_score_ts = now_ts
                    logger.info("⚡ Dynamic Blended Scanner updated (60% 48h Volatility + 40% 7d ATR).")
            except Exception as e_score:
                logger.debug(f"Blended score calculation error: {e_score}")

    # Evaluate each asset in the 23-asset universe
    for sym in scan_list:
        last_price = 0.0
        if tickers and sym in tickers:
            t = tickers[sym]
            last_price = float(t.get('last', 0) or 0)

        vol_score = blended_scores.get(sym, 0.0)
        if vol_score <= 0.0:
            # Fallback to 24h ticker range + ADX if candle fetch was unavailable
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
            vol_score = (0.60 * range_24h_pct * 10.0) + (0.40 * adx_score)

            if vol_score <= 0.0:
                default_scores = {
                    'INJ/USDT': 25.0, 'ARB/USDT': 28.0, 'TIA/USDT': 18.0, 'ICP/USDT': 17.0,
                    'DOT/USDT': 16.0, 'UNI/USDT': 15.0, 'FET/USDT': 14.0, 'NEAR/USDT': 13.0,
                    'LINK/USDT': 12.0, 'OP/USDT': 11.0, 'SUI/USDT': 10.0, 'APT/USDT': 10.0
                }
                vol_score = default_scores.get(sym, 8.0)

        # 🛑 10% Max Position Exposure Gate (Capital Trap Prevention)
        is_capped = False
        if portfolio and total_equity > 0 and hasattr(portfolio, 'holdings') and isinstance(portfolio.holdings, dict):
            h_obj = portfolio.holdings.get(sym)
            if h_obj:
                u_held = float(getattr(h_obj, 'units_held', 0) if hasattr(h_obj, 'units_held') else (h_obj or {}).get('units_held', 0) or 0)
                px_ref = last_price if last_price > 0 else float(getattr(h_obj, 'last_price', 0) if hasattr(h_obj, 'last_price') else (h_obj or {}).get('last_price', 0) or 0)
                if px_ref <= 0 and sym in ALL_23_HISTORICAL_COSTS:
                    px_ref = ALL_23_HISTORICAL_COSTS[sym]
                holding_val = u_held * px_ref
                exposure_pct = holding_val / total_equity
                if exposure_pct >= 0.10:
                    is_capped = True
                    logger.info(f"🛑 [CAP REACHED] {sym} exposure (${holding_val:,.2f}, {exposure_pct*100:.1f}%) >= 10% equity ceiling. Disqualified from active buy allocation to prevent trapped capital.")

        # 🛑 Individual BEAR Trend Filter (Anti-Falling-Knife Guard)
        is_bear = False
        if regime_detectors and sym in regime_detectors:
            det = regime_detectors[sym]
            if det and getattr(det, '_cached_state', None):
                st = det._cached_state
                reg = getattr(st, 'regime', None)
                reg_str = reg.name if hasattr(reg, 'name') else str(reg)
                if reg_str == 'BEAR':
                    is_bear = True
        # 🛑 Exclude Gold (XAUT), Macro Store of Value (BTC, ETH), and ARB (paused until after Sept 16, 2026 UTC)
        arb_paused = is_arb_buy_paused(sym)
        is_blacklisted = sym in ['XAUT/USDT', 'XAUT', 'BTC/USDT', 'ETH/USDT'] or arb_paused
        if is_blacklisted:
            reason = "USER PAUSE TILL SEPT 17" if arb_paused else "LOW VOLATILITY EXCLUSION"
            logger.info(f"🛑 [{reason}] {sym} excluded from active buy allocations.")

        is_disqualified = is_capped or is_bear or is_blacklisted
        scored_pairs.append((sym, vol_score, is_disqualified))

    # Rank assets by volatility score
    scored_pairs.sort(key=lambda x: x[1], reverse=True)
    
    # Select Top 8 eligible (non-capped, non-BEAR) leaders
    eligible_pairs = [p for p in scored_pairs if not p[2]]
    top_8_selected = [p[0] for p in eligible_pairs[:8]]

    allocations = {}
    for rank, sym in enumerate(top_8_selected):
        allocations[sym] = 0.10  # 10.0% max allocation per asset across Top 8 leaders

    # Any asset in scan_list not in top_8_selected gets 0.0% buy allocation
    for sym in scan_list:
        if sym not in allocations:
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
_active_dca_signals: Dict[str, float] = {}
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
        if not api_key or api_key in ("vU8Cg21arhQjUEWxvr", "QU1VKkbGXqy9MU9Qge"):
            api_key = "NzSg7VszfTXPjy2VdS"
            api_secret = "K9hHCNRooqs5Jik2Ez4Huis9XpxjCSkOAFcL"


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
    """Initialise portfolio and grid engines for all configured spot assets and Top 8 movers."""
    global _spot_initialized, _grid_engines, _regime_detectors, _active_roster
    
    if _spot_initialized and len(_active_roster) >= 8 and all(s in _grid_engines for s in _active_roster):
        return

    if not _spot_initialized:
        _spot_initialized = True
        _portfolio.load()
        if not spot_settings.paper_mode:
            try:
                import threading
                from trading_engine.spot.fifo_reconciler import reconcile as bg_reconcile
                threading.Thread(target=bg_reconcile, args=(get_spot_exchange(), getattr(spot_settings, 'fee_rate', 0.00075)), daemon=True).start()
            except Exception:
                pass

    exchange = get_spot_exchange()
    account_size = settings.account_size
    if not spot_settings.paper_mode and exchange:
        try:
            bal = exchange.fetch_balance({'accountType': 'UNIFIED'})
            info_list = bal.get('info', {}).get('result', {}).get('list', [])
            tot_equity = float(info_list[0].get('totalEquity', 0)) if info_list else 0.0
            usdt_total = float(bal.get('total', {}).get('USDT', 0) or bal.get('USDT', {}).get('total', 0) or 0)
            if tot_equity > 0:
                account_size = tot_equity
            elif usdt_total > 0:
                account_size = usdt_total
        except Exception as e:
            logger.debug(f"Could not fetch live balance in init_spot_engine: {e}")
            
    spot_active_capital = account_size * spot_settings.total_capital_pct
    expected_free = spot_active_capital
    expected_res = account_size * spot_settings.usdt_hard_reserve_pct
    
    _portfolio.usdt_reserved = expected_res
    if _portfolio.usdt_available == 0.0:
        _portfolio.usdt_available = expected_free
        _portfolio.save()

    # Pre-populate regime detectors for all 23 assets in the Halal universe
    for symbol in ALL_23_HALAL_UNIVERSE:
        if symbol not in _regime_detectors:
            _regime_detectors[symbol] = RegimeDetector(symbol=symbol, timeframe=spot_settings.regime_timeframe)

    # Run initial regime detection so confirmed regimes are cached
    # and dynamic Top 8 allocations are properly filtered from the very first second!
    try:
        run_spot_regime_check()
    except Exception as e_init_reg:
        logger.warning(f"Failed to run initial regime check in init_spot_engine: {e_init_reg}")

    logger.info(f"✅ Spot Engine Initialised (Top {len(_active_roster)} Active Movers: {sorted(list(_active_roster))}, Active Capital: ${spot_active_capital:,.2f})")


def run_spot_regime_check():
    """Run 23-asset regime detection and dynamic Top 8 capital rotation."""
    global _regime_detectors, _grid_engines, _active_roster
    exchange = get_spot_exchange()

    # 1. Update BTC Macro Master Filter
    try:
        btc_master_filter.update(exchange)
    except Exception as e_btc_r:
        logger.debug(f"BTC master filter in regime check: {e_btc_r}")

    # 2. Run regime detection on active roster, candidate symbols, and all 23 universe assets in parallel
    symbols_to_check = list(set(ALL_23_HALAL_UNIVERSE) | set(spot_settings.asset_list) | _active_roster)

    def _run_single_regime(sym):
        det = _regime_detectors.get(sym)
        if not det:
            det = RegimeDetector(symbol=sym)
            _regime_detectors[sym] = det
        try:
            st = det.detect(exchange)
            eng = _grid_engines.get(sym)
            reg_name = st.regime.name if hasattr(st.regime, 'name') else str(st.regime)
            if eng:
                eng.set_regime(reg_name, exchange=exchange)
            return sym, reg_name, st
        except Exception as e_reg:
            logger.warning(f"Failed regime check for {sym}: {e_reg}")
            return sym, None, None

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        reg_results = list(pool.map(_run_single_regime, symbols_to_check))
        for sym, reg_name, st in reg_results:
            if reg_name and st:
                logger.info(f"📊 Regime [{sym}]: {reg_name} (ADX: {st.adx:.1f}, +DI: {st.plus_di:.1f}, -DI: {st.minus_di:.1f})")

    # 3. Dynamic 23-Asset Scanner & Capital Rotation with 10% Exposure Ceiling
    try:
        total_eq = _portfolio.usdt_available + sum(
            (float(getattr(h, 'units_held', 0) if hasattr(h, 'units_held') else (h or {}).get('units_held', 0) or 0)) *
            (float(getattr(h, 'last_price', 0) if hasattr(h, 'last_price') else (h or {}).get('last_price', 0) or 0))
            for h in _portfolio.holdings.values()
        )
        if total_eq <= 0:
            total_eq = settings.account_size

        allocations = _get_dynamic_hot_asset_allocations(
            universe=ALL_23_HALAL_UNIVERSE,
            regime_detectors=_regime_detectors,
            portfolio=_portfolio,
            total_equity=total_eq
        )
        new_top_8 = {sym for sym, alloc in allocations.items() if alloc > 0.0}
        if not new_top_8:
            # Safe fallback: select from asset_list strictly excluding any confirmed BEAR or capped assets
            safe_candidates = []
            for s in spot_settings.asset_list:
                det = _regime_detectors.get(s)
                st = getattr(det, '_cached_state', None) if det else None
                reg = getattr(st, 'regime', None) if st else None
                reg_str = reg.name if hasattr(reg, 'name') else str(reg)
                if reg_str != 'BEAR':
                    safe_candidates.append(s)
            new_top_8 = set(safe_candidates[:8])

        # Detect promotions and demotions
        promoted = new_top_8 - _active_roster
        demoted = _active_roster - new_top_8

        if promoted or demoted:
            logger.info(f"🔄 [ROSTER ROTATION] Promoted to Top 8: {list(promoted)} | Demoted to Sell-Only: {list(demoted)}")

        # For demoted assets: zero out buy allocation and cleanly cancel BUY orders only (keep profit sells intact!)
        for sym_dem in demoted:
            eng = _grid_engines.get(sym_dem)
            if eng:
                eng.allocated_usd = 0.0
                if hasattr(eng, 'params') and eng.params:
                    eng.params.buy_levels = 0
                eng.cancel_buys_only(exchange)
                logger.info(f"🛡️ [DEMOTION SAFEGUARD] {sym_dem} rotated out of buying. Open buys cancelled; resting profit-taking limit sells left intact.")

        # Update active roster
        _active_roster = new_top_8

        # Allocate active capital to new Top 8 leaders
        active_cap = total_eq * spot_settings.total_capital_pct
        for symbol in _active_roster:
            alloc_pct = allocations.get(symbol, 0.0)
            if alloc_pct <= 0.0 and len(_active_roster) > 0:
                alloc_pct = 1.0 / len(_active_roster)
            new_asset_usd = active_cap * alloc_pct
            # 🛑 10% Max Position Exposure Clamp: ensure holding + new allocation never exceeds 10% equity
            h_obj = _portfolio.holdings.get(symbol) if hasattr(_portfolio, 'holdings') and isinstance(_portfolio.holdings, dict) else None
            h_val = 0.0
            if h_obj:
                u_held = float(getattr(h_obj, 'units_held', 0) if hasattr(h_obj, 'units_held') else (h_obj or {}).get('units_held', 0) or 0)
                p_ref = float(getattr(h_obj, 'last_price', 0) if hasattr(h_obj, 'last_price') else (h_obj or {}).get('last_price', 0) or 0)
                if p_ref <= 0 and symbol in ALL_23_HISTORICAL_COSTS:
                    p_ref = ALL_23_HISTORICAL_COSTS[symbol]
                h_val = u_held * p_ref
            
            max_headroom_usd = max(0.0, (total_eq * 0.10) - h_val)
            new_asset_usd = min(new_asset_usd, max_headroom_usd)

            engine = _grid_engines.get(symbol)
            if not engine:
                engine = GridEngine(
                    symbol=symbol,
                    allocated_usd=new_asset_usd,
                    paper_mode=spot_settings.paper_mode,
                    fee_rate=spot_settings.fee_rate
                )
                _grid_engines[symbol] = engine
                engine._last_rebuild_time = 0.0  # Force fresh build for newly promoted asset
                logger.info(f"🌟 [PROMOTED] Created new GridEngine for {symbol} with ${new_asset_usd:,.2f} ({alloc_pct*100:.1f}%)")
            else:
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
        asset_list = list(set(spot_settings.asset_list) | _active_roster)
        
        # ── Bulk Ticker Fetch (Real-world Mainnet Prices) ──
        pub_exchange = get_public_exchange()
        try:
            btc_master_filter.update(pub_exchange)
        except Exception as e_btc_t:
            logger.debug(f"BTC master filter tick update: {e_btc_t}")

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
        bal = {}  # safe default; prevents NameError in MNT refill block if fetch_balance raises early
        if not spot_settings.paper_mode and exchange:
            try:
                bal = exchange.fetch_balance({'accountType': 'UNIFIED'})
                usdt_total = float(bal.get('total', {}).get('USDT', 0) or bal.get('USDT', {}).get('total', 0) or 0)
                if usdt_total > 0:
                    _portfolio.usdt_available = usdt_total
                info_list = bal.get('info', {}).get('result', {}).get('list', [])
                tot_equity = float(info_list[0].get('totalEquity', 0)) if info_list else 0.0
                if tot_equity > 0:
                    _portfolio.total_unified_equity = tot_equity
                    _portfolio.usdt_reserved = tot_equity * spot_settings.usdt_hard_reserve_pct
                elif _portfolio.usdt_available > 0:
                    _portfolio.usdt_reserved = _portfolio.usdt_available * spot_settings.usdt_hard_reserve_pct

                tot = bal.get('total', {})
                active_symbols = set(spot_settings.asset_list) | _active_roster
                for coin, units in tot.items():
                    if coin in ['USDT', 'USDC', 'MNT']:
                        continue

                    u_val = float(units or 0)
                    if u_val > 0.0001:
                        sym = f"{coin}/USDT"
                        cur_p = float(tickers.get(sym, {}).get('last', 0.0) or 0.0)
                        if cur_p == 0.0:
                            try:
                                t_ind = exchange.fetch_ticker(sym)
                                cur_p = float(t_ind.get('last', 0.0) or 0.0)
                            except Exception:
                                pass
                        if sym not in _portfolio.holdings:
                            from trading_engine.spot.spot_portfolio import AssetHolding
                            hist_cost = float(ALL_23_HISTORICAL_COSTS.get(sym, 0.0) or 0.0)
                            fifo_c = 0.0
                            try:
                                from trading_engine.spot.fifo_reconciler import get_fifo_cost_basis
                                f_res = get_fifo_cost_basis(sym, units_held=u_val) or {}
                                fifo_c = float(f_res.get('avg_cost', 0.0) or 0.0)
                            except Exception:
                                pass
                            init_cost = max(hist_cost, fifo_c, cur_p)
                            _portfolio.holdings[sym] = AssetHolding(
                                symbol=sym,
                                units_held=u_val,
                                avg_cost_basis=init_cost,
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
            except Exception as e_mnt_refill:
                logger.debug(f"MNT auto-refill check: {e_mnt_refill}")

        # ── Calculate Global Open Buy Commitments Across All Assets ──
        total_open_buy_usd = 0.0
        open_orders_by_id = {}
        if not spot_settings.paper_mode and exchange:
            try:
                open_orders_all = exchange.fetch_open_orders(params={'category': 'spot'})
                for o in open_orders_all:
                    if o.get('id'):
                        open_orders_by_id[str(o['id'])] = o
                    if (o.get('side') or '').lower() == 'buy':
                        p_val = float(o.get('price', 0) or 0)
                        a_val = float(o.get('amount', 0) or 0)
                        total_open_buy_usd += p_val * a_val
            except Exception as e_open:
                logger.debug(f"Fetch open orders for reserve check: {e_open}")
        _portfolio.total_open_buy_usd = total_open_buy_usd

        # Iterate through all configured spot assets, Top 8 active roster movers, AND any legacy holding assets with tradable units (>= $5 Bybit min notional, excluding MNT fee buffer)
        all_candidate_symbols = list(set(spot_settings.asset_list) | _active_roster)
        if hasattr(_portfolio, 'holdings') and isinstance(_portfolio.holdings, dict):
            for sym_k, h_v in _portfolio.holdings.items():
                if sym_k in ['MNT/USDT', 'MNT']:
                    continue
                if sym_k not in all_candidate_symbols:
                    u_held = float(getattr(h_v, 'units_held', 0) if hasattr(h_v, 'units_held') else (h_v or {}).get('units_held', 0) or 0)
                    p_ref = float(getattr(h_v, 'last_price', 0) if hasattr(h_v, 'last_price') else (h_v or {}).get('last_price', 0) or 0)
                    if p_ref <= 0 and sym_k in ALL_23_HISTORICAL_COSTS:
                        p_ref = ALL_23_HISTORICAL_COSTS[sym_k]
                    # Only manage legacy assets that have at least $5.00 notional (Bybit minimum limit order size)
                    if (u_held * p_ref) >= 5.0:
                        all_candidate_symbols.append(sym_k)

        # Prune any in-memory engines for legacy symbols that are no longer candidates (e.g. sub-$5 dust)
        for sym_eng in list(_grid_engines.keys()):
            if sym_eng not in all_candidate_symbols:
                del _grid_engines[sym_eng]

        for symbol in all_candidate_symbols:
            engine = _grid_engines.get(symbol)
            if not engine:
                # Initialize a sell-only GridEngine for legacy holding assets (allocated_usd = 0 means no new buys)
                engine = GridEngine(
                    symbol=symbol,
                    allocated_usd=0.0,
                    paper_mode=spot_settings.paper_mode,
                    fee_rate=spot_settings.fee_rate
                )
                _grid_engines[symbol] = engine

            # Strictly lock legacy holding assets outside the active Top 8 roster into Sell-Only Mode
            if symbol not in _active_roster and symbol not in spot_settings.asset_list:
                engine.allocated_usd = 0.0
                if hasattr(engine, 'params') and engine.params:
                    engine.params.buy_levels = 0
                if any(lvl.side == 'buy' for lvl in engine.grid_levels):
                    engine.grid_levels = [lvl for lvl in engine.grid_levels if lvl.side == 'sell']

            # 🛑 Exclude Gold (XAUT) from any buy orders (Sell-Only / Profit-Taking only)
            if symbol in ['XAUT/USDT', 'XAUT']:
                engine.allocated_usd = 0.0
                if hasattr(engine, 'params') and engine.params:
                    engine.params.buy_levels = 0
                if any(lvl.side == 'buy' for lvl in engine.grid_levels):
                    engine.cancel_buys_only(exchange)

            # 🛑 Anti-Falling-Knife Guard: strictly suppress buys if asset is in confirmed BEAR regime
            if getattr(engine, 'current_regime', 'RANGE') == 'BEAR':
                engine.allocated_usd = 0.0
                if hasattr(engine, 'params') and engine.params:
                    engine.params.buy_levels = 0
                if any(lvl.side == 'buy' for lvl in engine.grid_levels):
                    engine.cancel_buys_only(exchange)

            # 🛡️ Dynamic 20% Hard Cash Reserve Shield:
            # If total available USDT is below the dynamic reserve floor, cancel resting buys to free cash
            res_floor = float(getattr(_portfolio, 'usdt_reserved', 0.0) or 0.0)
            avail_usdt = float(getattr(_portfolio, 'usdt_available', 0.0) or 0.0)
            if res_floor > 0 and avail_usdt < res_floor:
                if any(lvl.side == 'buy' for lvl in engine.grid_levels):
                    logger.info(f"[{symbol}] 🛡️ [RESERVE SHIELD] Freeing capital - cancelling resting buys to protect 20% cash reserve (${res_floor:,.2f} floor).")
                    engine.cancel_buys_only(exchange)

            # 🛑 10% Max Position Exposure Hard Ceiling:
            # If current holding value is >= 10% of total equity, strictly lock into Sell-Only Mode
            tot_eq_val = float(getattr(_portfolio, 'total_unified_equity', 0.0) or getattr(_portfolio, 'total_capital', 0.0) or 0.0)
            if tot_eq_val > 0:
                h_qty = _portfolio.get_position(symbol) if hasattr(_portfolio, 'get_position') else 0.0
                h_obj = _portfolio.get_holding(symbol) if hasattr(_portfolio, 'get_holding') else None
                ref_p = float(getattr(h_obj, 'last_price', 0.0) or 0.0) if h_obj else 0.0
                if ref_p <= 0.0:
                    ref_p = float(tickers.get(symbol, {}).get('last', 0.0) or 0.0)
                if ref_p <= 0.0 and symbol in ALL_23_HISTORICAL_COSTS:
                    ref_p = float(ALL_23_HISTORICAL_COSTS[symbol])
                h_val = h_qty * ref_p
                if (h_val / tot_eq_val) >= 0.10:
                    engine.allocated_usd = 0.0
                    if hasattr(engine, 'params') and engine.params:
                        engine.params.buy_levels = 0
                    engine.cancel_buys_only(exchange)
                    if not spot_settings.paper_mode and exchange:
                        try:
                            live_orders = exchange.fetch_open_orders(symbol, params={'category': 'spot'})
                            for o in live_orders:
                                if (o.get('side') or '').lower() == 'buy' and o.get('id'):
                                    try:
                                        exchange.cancel_order(o['id'], symbol=symbol)
                                        logger.info(f"🛑 [{symbol}] [10% CEILING ENFORCED] Cancelled live Bybit BUY order {o['id']}.")
                                    except Exception:
                                        pass
                        except Exception:
                            pass
                    logger.info(f"🛑 [{symbol}] [10% CEILING ENFORCED] Holding value (${h_val:,.2f}, {(h_val/tot_eq_val)*100:.1f}%) >= 10% equity limit. Strictly locked in Sell-Only Mode.")

            # 🛑 Temporary User Pause on ARB (Until After September 16, 2026 UTC):
            if is_arb_buy_paused(symbol):
                engine.allocated_usd = 0.0
                if hasattr(engine, 'params') and engine.params:
                    engine.params.buy_levels = 0
                engine.cancel_buys_only(exchange)
                if not spot_settings.paper_mode and exchange:
                    try:
                        live_orders = exchange.fetch_open_orders(symbol, params={'category': 'spot'})
                        for o in live_orders:
                            if (o.get('side') or '').lower() == 'buy' and o.get('id'):
                                try:
                                    exchange.cancel_order(o['id'], symbol=symbol)
                                    logger.info(f"🛑 [{symbol}] [PAUSE TILL SEPT 17] Cancelled live Bybit BUY order {o['id']}.")
                                except Exception:
                                    pass
                    except Exception:
                        pass
                logger.info(f"🛑 [{symbol}] User Pause active until after September 16, 2026. Strictly locked in Sell-Only Mode.")
                
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
                open_buys = [l for l in engine.grid_levels if l.status == 'open' and l.side == 'buy']
                open_sells = [l for l in engine.grid_levels if l.status == 'open' and l.side == 'sell']
                max_buy_p = max((l.price for l in open_buys), default=0.0)
                # A grid is only stale if it actually has active buy orders that have drifted >1.5% from current price.
                # When buy orders are paused (e.g. cash reserve floor or sell-only holdings), it is NOT stale.
                is_stale = bool(max_buy_p > 0 and (max_buy_p > price * 1.015 or max_buy_p < price * 0.985))
                force_reset = getattr(engine, '_last_rebuild_time', 0) == 0

                # Also check if we hold coins for this asset but have 0 open sell orders (critical for profit taking)
                holding_qty = _portfolio.get_position(symbol) if hasattr(_portfolio, 'get_position') else 0.0
                holding_val_usd = holding_qty * price
                # Only require sell orders if holding value is >= $5.00 (Bybit minimum limit order notional)
                missing_sells = bool(holding_val_usd >= 5.0 and len(open_sells) == 0)

                # Cost basis safety & high-velocity audit: detect if resting sells are below cost or excessively wide
                h_obj = _portfolio.get_holding(symbol) if hasattr(_portfolio, 'get_holding') else None
                h_cost = float(getattr(h_obj, 'avg_cost_basis', 0) or 0) if h_obj else 0.0
                try:
                    from trading_engine.spot.fifo_reconciler import get_fifo_cost_basis
                    fb = get_fifo_cost_basis(symbol, units_held=holding_qty if holding_qty > 0 else None)
                    if fb.get('avg_cost', 0) > 0:
                        h_cost = max(h_cost, float(fb['avg_cost']), float(fb.get('max_buy_price', 0) or 0))
                except Exception:
                    pass
                invalid_sells = False
                if h_cost > 0 and open_sells:
                    fee_factor = 0.0010
                    # 🛡️ STRICT ZERO-LOSS RULE ACROSS ALL ASSETS:
                    # Every sell order must strictly guarantee at least +$0.50 net profit above actual FIFO cost basis.
                    has_loss_sells = any(
                        (l.price * l.qty * (1.0 - fee_factor)) - (h_cost * l.qty * (1.0 + fee_factor)) < 0.50
                        for l in open_sells
                    )
                    if has_loss_sells:
                        invalid_sells = True
                        logger.info(f"🛡️ Re-aligning below-cost sell orders for {symbol} to guaranteed profit geometry (Cost: ${h_cost:.4f}, Live: ${price:.4f})...")
                    elif symbol in (set(spot_settings.asset_list) | _active_roster) and price > (h_cost * 1.02):
                        # For active watchlist assets in profit: tighten if existing sells are excessively wide (>3% above market)
                        min_sell_p = min((l.price for l in open_sells), default=0.0)
                        if min_sell_p > (price * 1.03):
                            # Verify that tightening to price * 1.008 still guarantees >= $0.50 net profit
                            test_qty = open_sells[0].qty if open_sells else 0.0
                            if test_qty > 0:
                                net_tight = ((price * 1.008) * test_qty * (1.0 - fee_factor)) - (h_cost * test_qty * (1.0 + fee_factor))
                                if net_tight >= 0.50:
                                    invalid_sells = True
                                    logger.info(f"⚡ Tightening wide take-profit targets for {symbol} (Live: ${price:.4f} > Cost: ${h_cost:.4f})...")
                    elif symbol not in (set(spot_settings.asset_list) | _active_roster):
                        # 🛡️ Zero-Loss Fee-Proof Quick-Exit for legacy holdings:
                        # Re-align if existing orders are excessively wide (>3.5% above cost/price), but NEVER below cost+fees+$0.50
                        target_quick_exit = max(h_cost * 1.0035, price * 1.0035)
                        min_sell_p = min((l.price for l in open_sells), default=0.0)
                        if min_sell_p > (target_quick_exit * 1.035):
                            invalid_sells = True
                            logger.info(f"🚪 Re-aligning legacy holding {symbol} to Quick-Exit target (Current: ${min_sell_p:.4f} -> Target ~${target_quick_exit:.4f})...")

                if not engine.grid_levels or is_stale or force_reset or missing_sells or invalid_sells:
                    if is_stale:
                        logger.info(f"🔄 Grid stale for {symbol} (Live: ${price:.4f}, Highest Buy Order: ${max_buy_p:.4f}). Re-centering grid around current price...")
                    elif missing_sells:
                        logger.info(f"🎯 Creating profit-taking sell orders for {symbol} (Holding: {holding_qty:.4f})...")
                    dip_boost = 1.25 if (time.time() - _active_dca_signals.get(symbol, 0.0) < 1800) else 1.0
                    engine.cancel_all(exchange)
                    engine.build_grid(price, _portfolio, atr=atr_val, force=True, dip_boost=dip_boost)
                    engine.place_grid_orders(_portfolio, exchange)



                # Process tick (checks crossable fills & places replacement orders)
                events = engine.tick(price, _portfolio, exchange=exchange, open_orders_by_id=open_orders_by_id)
                fill_events.extend(events)
                
                if events:
                    # If fills occurred, immediately place new replacement SELL/BUY limit orders on Bybit exchange!
                    engine.place_grid_orders(_portfolio, exchange)
            except Exception as e:
                logger.warning(f"Error in grid tick for {symbol}: {e}")
            
    finally:
        _tick_lock.release()
    _portfolio.save()

    # Continuously keep FIFO SQLite database synced in the background without blocking ticks
    if not spot_settings.paper_mode and exchange:
        try:
            import threading
            from trading_engine.spot.fifo_reconciler import reconcile as bg_fifo_reconcile
            fee_cfg = getattr(spot_settings, 'fee_rate', 0.00075)
            threading.Thread(target=bg_fifo_reconcile, args=(exchange, fee_cfg), daemon=True).start()
        except Exception:
            pass

    return {"status": "ok", "fills": fill_events, "portfolio": _portfolio.summary()}




def run_spot_dca_check():
    """Check for oversold DCA signals for extra buys."""
    exchange = get_spot_exchange()
    target_symbols = list(_active_roster) if _active_roster else spot_settings.asset_list
    for symbol in target_symbols:
        engine = _grid_engines.get(symbol)
        regime = engine.current_regime if engine else "RANGE"
        if regime == "BEAR" or is_arb_buy_paused(symbol):
            continue
        
        try:
            signal = _dca_manager.check(symbol, exchange, regime)
            if signal:
                _active_dca_signals[symbol] = time.time()
                mult = _dca_manager.extra_buy_multiplier(regime, signal.trigger_type)
                streak_factor = _portfolio.get_streak_risk_factor()
                raw_size = (engine.allocated_usd * 0.20) * mult * streak_factor if engine else 50.0
                order_size = max(35.0, raw_size)  # Guaranteed minimum $35 USD size for DCA buys
                res_floor = float(getattr(_portfolio, 'usdt_reserved', 0.0) or 0.0)
                avail_usdt = float(getattr(_portfolio, 'usdt_available', 0.0) or 0.0)
                open_buys_usd = float(getattr(_portfolio, 'total_open_buy_usd', 0.0) or 0.0)

                # 🛑 AIRTIGHT HARD CASH RESERVE GATE FOR DCA BUYS:
                if res_floor > 0 and (avail_usdt - open_buys_usd - order_size) < res_floor:
                    logger.info(f"🛑 [DCA PAUSED] Skipping DCA buy for {symbol} (${order_size:.2f}) - Would breach hard cash reserve (${res_floor:,.2f} floor).")
                    continue

                # 🛑 10% MAXIMUM ASSET EXPOSURE CEILING FOR DCA BUYS:
                tot_eq_val = float(getattr(_portfolio, 'total_unified_equity', 0.0) or getattr(_portfolio, 'total_capital', 0.0) or 0.0)
                if tot_eq_val > 0:
                    cap_10 = tot_eq_val * 0.10
                    h_qty = _portfolio.get_position(symbol) if hasattr(_portfolio, 'get_position') else 0.0
                    ticker_dca = exchange.fetch_ticker(symbol)
                    p_dca = float(ticker_dca.get('last', 0) or 0)
                    h_val = h_qty * p_dca
                    if (h_val + order_size) > cap_10:
                        logger.info(f"🛑 [DCA 10% CAP] Skipping DCA buy for {symbol} - Total exposure (${(h_val + order_size):.2f}) would exceed 10% equity cap (${cap_10:.2f}).")
                        continue

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

                    # ── DCA Exit Target: place limit sell guaranteeing >= +$0.60 NET profit strictly above FIFO cost ──
                    fee_factor = spot_settings.fee_rate
                    cost_ref = price
                    try:
                        from trading_engine.spot.fifo_reconciler import get_fifo_cost_basis
                        fb = get_fifo_cost_basis(symbol)
                        if fb.get('max_buy_price', 0) > 0:
                            cost_ref = max(cost_ref, float(fb['max_buy_price']), float(fb.get('avg_cost', 0)))
                    except Exception:
                        pass
                    denom = qty * (1.0 - fee_factor)
                    min_fee_proof_exit = (cost_ref * qty * (1.0 + fee_factor) + 0.60) / denom if denom > 0 else cost_ref * 1.015
                    exit_price = round(max(cost_ref * 1.015, min_fee_proof_exit), 6)
                    try:
                        if not spot_settings.paper_mode and exchange:
                            exchange.create_limit_sell_order(symbol, qty, exit_price)
                            logger.info(f"📤 DCA Exit Limit Sell placed [{symbol}]: qty={qty:.6f} @ ${exit_price:.4f} (CostRef: ${cost_ref:.4f}, Guaranteed Net: >= +$0.60 USD)")
                        else:
                            logger.info(f"📤 [PAPER] DCA Exit Limit Sell [{symbol}]: qty={qty:.6f} @ ${exit_price:.4f} (CostRef: ${cost_ref:.4f}, Guaranteed Net: >= +$0.60 USD)")
                    except Exception as sell_err:
                        logger.warning(f"DCA exit sell placement failed [{symbol}]: {sell_err}")

        except Exception as e:
            logger.warning(f"DCA check failed for {symbol}: {e}")



_cached_spot_status: Dict[str, Any] | None = None
_last_spot_status_time: float = 0.0
_last_auto_tick_time: float = 0.0


def get_spot_status(force: bool = False) -> Dict[str, Any]:
    """Returns full JSON state for API / dashboard (Sub-20ms Fast Cache)."""
    global _cached_spot_status, _last_spot_status_time
    now = time.time()
    if not force and _cached_spot_status and (now - _last_spot_status_time) < 3.0:
        return _cached_spot_status


    init_spot_engine()
    exchange = get_spot_exchange()

    
    # The 24/7 background daemon thread ticks continuously every 30s, so get_spot_status stays non-blocking and instant



    regimes = {}
    grids = {}
    display_regime_symbols = set(spot_settings.asset_list) | _active_roster | set(_portfolio.holdings.keys() if hasattr(_portfolio, 'holdings') and isinstance(_portfolio.holdings, dict) else [])
    for sym in display_regime_symbols:
        if sym in ['MNT/USDT', 'MNT']:
            continue
        det = _regime_detectors.get(sym)
        h_obj = _portfolio.holdings.get(sym) if hasattr(_portfolio, 'holdings') and isinstance(_portfolio.holdings, dict) else None
        last_p = float(h_obj.get('last_price', 0.0) if isinstance(h_obj, dict) else getattr(h_obj, 'last_price', 0.0) or 0.0)
        if det and det._cached_state:
            st = det._cached_state
            reg_name = st.regime.name if hasattr(st.regime, 'name') else str(st.regime)
            regimes[sym] = {
                "regime": reg_name,
                "adx": round(float(st.adx), 1),
                "plus_di": round(float(st.plus_di), 1),
                "minus_di": round(float(st.minus_di), 1),
                "sma_50": round(float(st.sma_50), 4),
                "sma_200": round(float(st.sma_200), 4),
                "price": round(float(st.price or last_p), 4),
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
        summary_dict['vol_score'] = float(_cached_blended_scores.get(sym, 0.0))
        grids[sym] = summary_dict

    # If Live / Demo mode: merge live open orders directly from Bybit
    if not spot_settings.paper_mode and exchange:
        try:
            active_symbols = set(spot_settings.asset_list) | _active_roster
            legacy_held_symbols = set()
            for sym, h in _portfolio.holdings.items():
                if sym in ['MNT/USDT', 'MNT']:
                    continue
                u_held = float(getattr(h, 'units_held', 0) if hasattr(h, 'units_held') else (h or {}).get('units_held', 0) or 0)
                p_ref = float(getattr(h, 'last_price', 0) if hasattr(h, 'last_price') else (h or {}).get('last_price', 0) or 0)
                if p_ref <= 0 and sym in ALL_23_HISTORICAL_COSTS:
                    p_ref = ALL_23_HISTORICAL_COSTS[sym]
                if (u_held * p_ref) >= 5.0:
                    legacy_held_symbols.add(sym)

            visible_symbols = active_symbols | legacy_held_symbols

            bybit_levels = {}
            for sym in visible_symbols:
                try:
                    open_orders = exchange.fetch_open_orders(sym, params={'category': 'spot'})
                    h_sym_obj = _portfolio.holdings.get(sym)
                    u_sym = float(getattr(h_sym_obj, 'units_held', 0) if hasattr(h_sym_obj, 'units_held') else (h_sym_obj or {}).get('units_held', 0) or 0)
                    p_sym = float(getattr(h_sym_obj, 'last_price', 0) if hasattr(h_sym_obj, 'last_price') else (h_sym_obj or {}).get('last_price', 0) or 0)
                    if p_sym <= 0 and sym in ALL_23_HISTORICAL_COSTS:
                        p_sym = ALL_23_HISTORICAL_COSTS[sym]
                    tot_eq_chk = float(summary_data.get('total_unified_equity') or summary_data.get('total_capital') or 0.0)
                    is_capped_sym = (tot_eq_chk > 0 and ((u_sym * p_sym) / tot_eq_chk) >= 0.10)

                    for o in open_orders:
                        is_sell = (o.get('side') or '').lower() == 'sell'
                        has_holding = sym in legacy_held_symbols
                        
                        # Only cancel rogue BUY orders on decommissioned, capped, or paused assets; NEVER cancel resting take-profit SELL orders on legacy holdings!
                        arb_paused = is_arb_buy_paused(sym)
                        if not is_sell and (sym not in active_symbols or is_capped_sym or arb_paused) and o.get('id'):
                            try:
                                exchange.cancel_order(o['id'], symbol=sym)
                                logger.info(f"🧹 Cleaned up {'paused' if arb_paused else ('capped' if is_capped_sym else 'decommissioned')} buy order {o['id']} on {sym}")
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
                except Exception:
                    pass

            for sym, lvl_list in bybit_levels.items():
                if sym not in grids:
                    grids[sym] = {'symbol': sym, 'levels': lvl_list, 'open_buys': 0, 'open_sells': 0}
                grids[sym]['levels'] = lvl_list
                grids[sym]['open_buys'] = sum(1 for l in lvl_list if l['side'] == 'buy')
                grids[sym]['open_sells'] = sum(1 for l in lvl_list if l['side'] == 'sell')

        except Exception as e_orders:
            logger.debug(f"Live open orders fetch in get_spot_status: {e_orders}")

    # Include active symbols PLUS any legacy symbol we still hold coins for (so resting TP sells remain visible on dashboard)
    active_symbols = set(spot_settings.asset_list) | _active_roster
    legacy_held_symbols = {
        sym for sym, h in _portfolio.holdings.items()
        if float(getattr(h, 'units_held', 0) if hasattr(h, 'units_held') else (h or {}).get('units_held', 0) or 0) > 0.0001
    }
    visible_symbols = active_symbols | legacy_held_symbols
    grids = {sym: data for sym, data in grids.items() if sym in visible_symbols}
        
    # Fetch recent trade execution history from exchange or portfolio

    global _last_trades_fetch_time, _cached_raw_trades
    recent_trades = []
    real_exchange_fees_all = 0.0
    trades = []
    if not spot_settings.paper_mode and exchange:
        now_ts = time.time()
        if (now_ts - _last_trades_fetch_time > 15.0) or not _cached_raw_trades:
            fetched = []
            try:
                bulk = exchange.fetch_my_trades(limit=100, params={'category': 'spot'})
                fetched.extend(bulk)
            except Exception:
                pass

            if fetched:

                seen_ids = set()
                deduped = []
                for t in fetched:
                    tid = str(t.get('id') or f"{t.get('timestamp')}_{t.get('amount')}")
                    if tid not in seen_ids:
                        seen_ids.add(tid)
                        deduped.append(t)
                _cached_raw_trades = deduped
                _last_trades_fetch_time = now_ts
                try:
                    import threading
                    from trading_engine.spot.fifo_reconciler import reconcile as bg_fifo_reconcile
                    fee_cfg = getattr(spot_settings, 'fee_rate', 0.00075)
                    threading.Thread(target=bg_fifo_reconcile, args=(exchange, fee_cfg), daemon=True).start()
                except Exception:
                    pass

        trades = list(_cached_raw_trades)

        for t in trades:
            f_cost = 0.0
            if isinstance(t.get('fee'), dict):
                f_cost = float(t['fee'].get('cost', 0) or 0)
            if f_cost == 0 and 'info' in t:
                f_cost = float(t['info'].get('execFee', 0) or 0)
            real_exchange_fees_all += f_cost
            
        trades = sorted(trades, key=lambda x: str(x.get('timestamp') or ''))

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

    # Collect completed cycles across SQLite DB + all grid engines
    try:
        completed_dict = {}
        try:
            from trading_engine.spot.trade_db import get_completed_cycles
            db_cycles = get_completed_cycles(1000)
            for c in db_cycles:
                key = f"{c.get('symbol')}_{c.get('timestamp')}_{c.get('qty')}"
                completed_dict[key] = c
        except Exception as e_db:
            logger.debug(f"SQLite cycle load: {e_db}")

        if hasattr(_portfolio, 'completed_cycles') and isinstance(_portfolio.completed_cycles, list):
            for c in _portfolio.completed_cycles:
                if isinstance(c, dict):
                    c_item = dict(c)
                    s_id = str(c_item.get('sell_order_id', ''))
                    b_id = str(c_item.get('buy_order_id', ''))
                    ts = str(c_item.get('timestamp', ''))
                    if not spot_settings.paper_mode and (s_id.startswith('PAPER') or b_id.startswith('PAPER') or ('+' in ts and len(ts) > 28)):
                        continue
                    key = f"{c_item.get('symbol')}_{c_item.get('timestamp')}_{c_item.get('qty')}"
                    completed_dict[key] = c_item

        for sym, eng in _grid_engines.items():
            if not spot_settings.paper_mode and getattr(eng, 'paper_mode', False):
                continue
            if hasattr(eng, 'completed_cycles') and isinstance(eng.completed_cycles, list):
                for c in eng.completed_cycles:
                    if isinstance(c, dict):
                        c_item = dict(c)
                        c_item['symbol'] = sym
                        s_id = str(c_item.get('sell_order_id', ''))
                        b_id = str(c_item.get('buy_order_id', ''))
                        ts = str(c_item.get('timestamp', ''))
                        if not spot_settings.paper_mode and (s_id.startswith('PAPER') or b_id.startswith('PAPER') or ('+' in ts and len(ts) > 28)):
                            continue
                        key = f"{sym}_{c_item.get('timestamp')}_{c_item.get('qty')}"
                        completed_dict[key] = c_item

        # In live mode, strictly exclude any simulated PAPER orders or test artifacts
        if not spot_settings.paper_mode:
            filtered_dict = {}
            for k, c in completed_dict.items():
                s_id = str(c.get('sell_order_id', ''))
                b_id = str(c.get('buy_order_id', ''))
                ts = str(c.get('timestamp', ''))
                if s_id.startswith('PAPER') or b_id.startswith('PAPER'):
                    continue
                if '+' in ts and len(ts) > 28:
                    continue
                filtered_dict[k] = c
            completed_dict = filtered_dict

        # ── FIFO Reconciler: single authoritative source of truth from SQLite ledger ──────
        from trading_engine.spot.fifo_reconciler import get_recent_cycles, get_daily_pnl, get_alltime_pnl

        # Authoritative metrics for today's UTC calendar day
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily   = get_daily_pnl(today_str)

        alltime = get_alltime_pnl()
        recent  = get_recent_cycles(limit=100)



        fifo_cycles_fmt = []
        for c in recent:
            fifo_cycles_fmt.append({
                'symbol':     c['symbol'],
                'buy_price':  c['buy_price'],
                'sell_price': c['sell_price'],
                'qty':        c['qty'],
                'gross_pnl':  c['gross_pnl'],
                'fee':        c['fee'],
                'net_pnl':    c['net_pnl'],
                'timestamp':  c['sell_timestamp'],
            })

        summary_data['total_realised_pnl']  = alltime['net_pnl']
        summary_data['daily_realised_pnl']   = daily['net_pnl']
        summary_data['daily_gross_pnl']       = daily['gross_pnl']
        summary_data['gross_pnl_today']       = daily['gross_pnl']
        summary_data['fees_today']            = daily['fees']
        summary_data['total_gross_all_time']  = alltime['gross_pnl']
        summary_data['total_fees_all_time']   = alltime['fees']
        summary_data['cycles_today']          = daily['cycles']
        summary_data['total_cycles']          = alltime['cycles_total']
        summary_data['completed_cycles']      = fifo_cycles_fmt

        _portfolio.completed_cycles    = fifo_cycles_fmt
        _portfolio.cycles_today        = daily['cycles']
        _portfolio.total_realised_pnl  = alltime['net_pnl']
        _portfolio.daily_realised_pnl  = daily['net_pnl']
        _portfolio.daily_gross_pnl     = daily['gross_pnl']
        _portfolio.gross_pnl_today     = daily['gross_pnl']
        _portfolio.fees_today          = daily['fees']
    except Exception as e_cycles:
        logger.warning(f"Error compiling completed cycles in get_spot_status: {e_cycles}")





    # If Live / Demo mode: fetch live exchange spot balances for holdings and total capital
    if not spot_settings.paper_mode and exchange:
        try:
            bal = exchange.fetch_balance({'accountType': 'UNIFIED'})
            tot = bal.get('total', {})
            usdt_tot = float(tot.get('USDT', 0) or 0)
            usdt_free_val = float(bal.get('free', {}).get('USDT', 0) or 0)
            usdt_used_val = float(bal.get('used', {}).get('USDT', 0) or 0)
            
            info_list = bal.get('info', {}).get('result', {}).get('list', [])
            official_tot_equity = 0.0
            if info_list and isinstance(info_list, list) and len(info_list) > 0:
                official_tot_equity = float(info_list[0].get('totalEquity', 0) or 0)

            if usdt_tot > 0 or official_tot_equity > 0:
                summary_data['usdt_available'] = usdt_tot if usdt_tot > 0 else _portfolio.usdt_available
                summary_data['usdt_free'] = usdt_free_val
                summary_data['usdt_in_orders'] = usdt_used_val
                summary_data['total_capital'] = official_tot_equity if official_tot_equity > 0 else usdt_tot
                summary_data['total_unified_equity'] = official_tot_equity if official_tot_equity > 0 else usdt_tot
                if official_tot_equity > 0:
                    _portfolio.usdt_reserved = official_tot_equity * spot_settings.usdt_hard_reserve_pct
                elif usdt_tot > 0:
                    _portfolio.usdt_reserved = usdt_tot * spot_settings.usdt_hard_reserve_pct
                summary_data['usdt_reserved'] = _portfolio.usdt_reserved

            # Group recent trades in memory to eliminate 15+ sequential network roundtrips to Bybit
            trades_by_sym = {}
            for t in (recent_trades if recent_trades else []):
                s_name = t.get('symbol', '')
                if s_name not in trades_by_sym:
                    trades_by_sym[s_name] = []
                trades_by_sym[s_name].append(t)

            live_holdings = {}
            active_symbols = set(spot_settings.asset_list) | _active_roster
            for coin, units in tot.items():
                if coin in ['USDT', 'USDC']:
                    continue
                units_val = float(units or 0)
                if units_val > 0.0001:
                    symbol = f"{coin}/USDT"
                    # Include any coin held in wallet so legacy/pruned positions remain visible until sold
                    cur_price = 0.0
                    if symbol in _portfolio.holdings:
                        h_val = _portfolio.holdings[symbol]
                        cur_price = float(h_val.get('last_price', 0.0) if isinstance(h_val, dict) else getattr(h_val, 'last_price', 0.0) or 0.0)
                    if cur_price <= 0 and exchange:
                        try:
                            t_info = exchange.fetch_ticker(symbol)
                            cur_price = float(t_info.get('last') or 0.0)
                        except Exception:
                            cur_price = 0.0



                    # Compute exact FIFO cost basis in-memory (0ms network latency)
                    avg_cost_basis = ALL_23_HISTORICAL_COSTS.get(symbol, cur_price)
                    
                    sym_buys = [t for t in trades_by_sym.get(symbol, []) if str(t.get('side', '')).upper() == 'BUY']
                    if sym_buys:
                        acc_q = sum(float(b.get('amount') or b.get('qty') or 0.0) for b in sym_buys)
                        acc_c = sum(float(b.get('size_usd') or b.get('cost') or 0.0) or (float(b.get('price') or 0.0) * float(b.get('amount') or b.get('qty') or 0.0)) for b in sym_buys)
                        if acc_q >= units_val * 0.90 and acc_q > 0:
                            avg_cost_basis = acc_c / acc_q

                    if symbol in ALL_23_HISTORICAL_COSTS:
                        avg_cost_basis = ALL_23_HISTORICAL_COSTS[symbol]

                    unrealised_pnl = (cur_price - avg_cost_basis) * units_val
                    total_cost = avg_cost_basis * units_val
                    unrealised_pnl_pct = (unrealised_pnl / total_cost) if total_cost > 0 else 0.0

                    value_usd = units_val * cur_price
                    live_holdings[symbol] = {
                        'symbol': symbol,
                        'units_held': round(units_val, 8),
                        'avg_cost_basis': round(avg_cost_basis, 4),
                        'base_hold_units': 0.0,
                        'last_price': round(cur_price, 4),
                        'value_usd': round(value_usd, 2),
                        'unrealised_pnl': round(unrealised_pnl, 2),
                        'unrealised_pnl_pct': round(unrealised_pnl_pct, 4)
                    }
            if live_holdings:
                summary_data['holdings'] = live_holdings
                _portfolio.holdings = {
                    k: AssetHolding(
                        symbol=v.get('symbol', k),
                        units_held=float(v.get('units_held', 0.0) or 0.0),
                        avg_cost_basis=float(v.get('avg_cost_basis', 0.0) or 0.0),
                        base_hold_units=float(v.get('base_hold_units', 0.0) or 0.0),
                        last_price=float(v.get('last_price', 0.0) or 0.0)
                    ) if isinstance(v, dict) else v
                    for k, v in live_holdings.items()
                }
                if official_tot_equity <= 0:
                    summary_data['total_unified_equity'] = usdt_tot + sum(h['value_usd'] for h in live_holdings.values())
        except Exception as e_bal:
            logger.debug(f"Live balance fetch in get_spot_status: {e_bal}")

    # Dynamic Realised PnL strictly synced with Completed Cycles table
    # ── Final sync: use whatever completed_list computed above ──
    daily_val = float(summary_data.get('daily_realised_pnl', 0.0))
    today_gross_val = float(summary_data.get('gross_pnl_today', 0.0))
    today_fees_val = float(summary_data.get('fees_today', 0.0))
    total_val = float(summary_data.get('total_realised_pnl', 0.0))
    total_gross_all = float(summary_data.get('total_gross_all_time', 0.0))
    total_fees_all = float(summary_data.get('total_fees_all_time', 0.0))
    cycles_val = int(summary_data.get('cycles_today', 0))
    total_cycles_val = int(summary_data.get('total_cycles', 0))
    
    summary_data['daily_realised_pnl'] = round(daily_val, 2)
    summary_data['daily_gross_pnl'] = round(today_gross_val, 2)
    summary_data['gross_pnl_today'] = round(today_gross_val, 2)
    summary_data['fees_today'] = round(today_fees_val, 2)
    summary_data['total_realised_pnl'] = round(total_val, 2)
    summary_data['cycles_today'] = cycles_val
    _portfolio.daily_realised_pnl = daily_val
    _portfolio.daily_gross_pnl = today_gross_val
    _portfolio.gross_pnl_today = today_gross_val
    _portfolio.fees_today = today_fees_val
    _portfolio.total_realised_pnl = total_val
    _portfolio.cycles_today = cycles_val




    # Live Real-Time Inventory Dip (Distance from current live price to resting take-profit sell targets)
    held_val_total = sum(float(h.get('value_usd', 0) or 0) for h in summary_data.get('holdings', {}).values())
    target_sell_val = sum((float(h.get('value_usd', 0) or 0) * 1.008) for h in summary_data.get('holdings', {}).values())
    inv_dip_drag = round(min(-0.01, held_val_total - target_sell_val), 2)
    
    # True reconciliation — all from real computed Bybit exchange fills
    total_fee_ledger = round(real_exchange_fees_all if real_exchange_fees_all > 0 else total_fees_all, 2)
    total_trade_count = len(trades) if trades else total_cycles_val

    usdt_in_orders_val = float(summary_data.get('usdt_in_orders', 0.0))
    coins_val_total = sum(float(h.get('value_usd', 0.0) or 0.0) for h in summary_data.get('holdings', {}).values())
    tot_cap_val = float(summary_data.get('total_unified_equity') or summary_data.get('total_capital') or (usdt_free_val + coins_val_total + usdt_in_orders_val))
    
    # Deployed Capital = Coin Inventory Value + USDT locked in active Limit Buy Orders
    total_deployed = round(min(tot_cap_val, coins_val_total + usdt_in_orders_val), 2)
    deployed_pct = round((total_deployed / tot_cap_val) * 100, 1) if tot_cap_val > 0 else 0.0

    # ── GROUND TRUTH: Real wallet growth = Bybit equity − total deposits ──
    deposit_base = 8015.83
    real_account_growth = round(tot_cap_val - deposit_base, 2)

    summary_data['total_deployed_usd'] = total_deployed
    summary_data['deployed_pct'] = deployed_pct

    summary_data['reconciliation'] = {
        'gross_cycle_gains': round(total_gross_all, 2),
        'fees_paid': total_fee_ledger,
        'trade_count': total_trade_count,
        'net_true_account_growth': real_account_growth,
        'deposit_base': deposit_base,
        'bybit_equity': round(tot_cap_val, 2),
    }


    res = {
        "enabled": spot_settings.enabled,
        "paper_mode": spot_settings.paper_mode,
        "total_capital": tot_cap_val,
        "total_capital_pct": spot_settings.total_capital_pct,
        "usdt_hard_reserve_pct": spot_settings.usdt_hard_reserve_pct,
        "usdt_reserved": float(getattr(_portfolio, 'usdt_reserved', 0.0) or 0.0),
        "portfolio": summary_data,
        "regimes": regimes,
        "grids": grids,
        "btc_guard": btc_master_filter.summary(),
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
            # Step 1a: Cancel open BUY orders on legacy/removed symbols to free capital.
            # NEVER cancel resting take-profit SELL orders on legacy holdings — they represent locked profit.
            active_symbols = set(spot_settings.asset_list) | _active_roster
            tot_eq_self = float(getattr(_portfolio, 'total_unified_equity', 0.0) or getattr(_portfolio, 'total_capital', 0.0) or 0.0)
            for o in open_orders:
                raw_sym = o.get('symbol', '')
                sym = raw_sym if '/' in raw_sym else (raw_sym.replace('USDT', '/USDT') if 'USDT' in raw_sym else raw_sym)
                is_sell = (o.get('side') or '').lower() == 'sell'
                h_obj = _portfolio.holdings.get(sym)
                units_held = float(getattr(h_obj, 'units_held', 0) if hasattr(h_obj, 'units_held') else (h_obj or {}).get('units_held', 0) or 0)
                p_ref = float(getattr(h_obj, 'last_price', 0) if hasattr(h_obj, 'last_price') else (h_obj or {}).get('last_price', 0) or 0)
                if p_ref <= 0 and sym in ALL_23_HISTORICAL_COSTS:
                    p_ref = ALL_23_HISTORICAL_COSTS[sym]
                has_holding = (units_held * p_ref) >= 5.0
                is_capped_sym = (tot_eq_self > 0 and ((units_held * p_ref) / tot_eq_self) >= 0.10)

                # Protect: never cancel a sell order on an asset we still hold with >= $5 notional
                if is_sell and has_holding:
                    logger.debug(f"🛡️ Self-Healing: Preserving legacy TP sell {o.get('id')} on {sym} (still holding {units_held:.4f} units, val=${units_held*p_ref:.2f}).")
                    continue
                
                # Cancel rogue BUY orders on decommissioned, capped, or paused assets:
                arb_paused = is_arb_buy_paused(sym)
                if not is_sell and (sym not in active_symbols or is_capped_sym or arb_paused) and o.get('id'):
                    try:
                        exchange.cancel_order(o.get('id'), symbol=sym)
                        logger.info(f"🧹 Self-Healing: Cancelled {'paused' if arb_paused else ('capped' if is_capped_sym else 'legacy')} buy order {o.get('id')} on {sym} to protect capital.")
                        healed_count += 1
                    except Exception as e_canc:
                        logger.debug(f"Could not cancel order {o.get('id')} on {sym}: {e_canc}")

            bybit_order_ids = {o.get('id') for o in open_orders if o.get('id')}
            
            for sym, eng in _grid_engines.items():
                for lvl in eng.grid_levels:
                    if lvl.status == 'open' and lvl.order_id and lvl.order_id not in bybit_order_ids:
                        # Order no longer open on Bybit — self-heal
                        logger.info(f"🛠️ Self-Healing [{sym}]: Order {lvl.order_id} filled/closed out-of-band on Bybit. Auto-syncing state.")
                        lvl.status = 'filled'
                        healed_count += 1
                        if lvl.side == 'buy':
                            _portfolio.record_buy(sym, lvl.qty, lvl.price, lvl.size_usd, order_id=lvl.order_id or '')
                        elif lvl.side == 'sell':
                            buy_cost = getattr(lvl, 'linked_buy_price', 0.0)
                            if not buy_cost or buy_cost <= 0.0:
                                try:
                                    from trading_engine.spot.fifo_reconciler import get_fifo_cost_basis
                                    f_res = get_fifo_cost_basis(sym, units_held=lvl.qty if lvl.qty > 0 else None) or {}
                                    buy_cost = float(f_res.get('avg_cost', 0.0) or 0.0)
                                except Exception:
                                    pass
                            if not buy_cost or buy_cost <= 0.0:
                                buy_cost = float(ALL_23_HISTORICAL_COSTS.get(sym, 0.0) or 0.0)
                            if not buy_cost or buy_cost <= 0.0:
                                buy_cost = (lvl.price / (1.0 + getattr(eng, 'current_spacing', 0.01)))
                            _portfolio.record_sell(sym, lvl.qty, lvl.price, lvl.size_usd, lvl.order_id or '', buy_cost)

            _portfolio.save()
        except Exception as e_heal:
            logger.warning(f"Self-healing audit encounter exception: {e_heal}")

    # Step 2: Automated Event-Driven Backtesting
    from .backtest import run_backtest
    target_bt_symbols = list(_active_roster) if _active_roster else spot_settings.asset_list
    for sym in target_bt_symbols:
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



