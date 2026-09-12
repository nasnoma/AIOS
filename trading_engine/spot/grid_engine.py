from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
import json
import math
from pathlib import Path
import ccxt
from datetime import datetime, timezone
from loguru import logger
from uuid import uuid4

# Path where auto_optimizer.py writes winning params
_BEST_PARAMS_FILE = Path(__file__).parent / 'best_params.json'

# 🛑 User Pause on ARB: strictly prevent buying ARB until after September 23, 2026 UTC (resumes Sept 24 00:00 UTC) — covers mid/late-Sep unlock window
ARB_PAUSE_UNTIL_UTC = datetime(2026, 9, 24, 0, 0, 0, tzinfo=timezone.utc)  # keep in sync with unlock_calendar.ARB_HARD_PAUSE_UNTIL_UTC

from trading_engine.spot.asset_guards import is_never_sell_symbol, is_fee_buffer_asset, is_never_buy_symbol
from trading_engine.spot.sell_guard import (
    resolve_sell_cost_ref,
    min_fee_proof_sell_price,
    sell_clears_buy,
    sell_is_fee_proof,
    enforce_sell_floor,
    get_fee_factor,
    estimated_net_pnl,
)

def is_arb_buy_paused(symbol: str) -> bool:
    """True if buys are paused for unlock risk (ARB/TIA calendar + ARB hard floor)."""
    try:
        from trading_engine.spot.unlock_calendar import is_unlock_buy_paused
        paused, _reason = is_unlock_buy_paused(symbol)
        if paused:
            return True
    except Exception:
        pass
    # Fallback hard floor if calendar import fails
    if symbol in ('ARB/USDT', 'ARB', 'TIA/USDT', 'TIA'):
        if symbol in ('ARB/USDT', 'ARB') and datetime.now(timezone.utc) < ARB_PAUSE_UNTIL_UTC:
            return True
    return False

@dataclass
class RegimeParams:
    grid_spacing: float
    buy_levels: int
    sell_levels: int
    capital_pct: float
    base_hold_pct: float

def _load_regime_params() -> Dict[str, RegimeParams]:
    """Prefer SpotGridSettings (Railway/env) over hardcoded defaults so live knobs stay in sync."""
    try:
        from trading_engine.config import spot_settings as ss
        return {
            'BULL': RegimeParams(
                grid_spacing=float(ss.bull_grid_spacing),
                buy_levels=int(ss.bull_buy_levels),
                sell_levels=int(ss.bull_sell_levels),
                capital_pct=float(ss.bull_capital_deployed),
                base_hold_pct=float(ss.bull_base_hold_pct),
            ),
            'RANGE': RegimeParams(
                grid_spacing=float(ss.range_grid_spacing),
                buy_levels=int(ss.range_buy_levels),
                sell_levels=int(ss.range_sell_levels),
                capital_pct=float(ss.range_capital_deployed),
                base_hold_pct=float(ss.range_base_hold_pct),
            ),
            'BEAR': RegimeParams(
                grid_spacing=float(ss.bear_grid_spacing),
                buy_levels=0,  # hard: no new buys in BEAR regardless of config
                sell_levels=int(ss.bear_sell_levels),
                capital_pct=0.0,
                base_hold_pct=float(ss.bear_base_hold_pct),
            ),
        }
    except Exception:
        return {
            'BULL': RegimeParams(grid_spacing=0.008, buy_levels=4, sell_levels=6, capital_pct=0.85, base_hold_pct=0.30),
            'RANGE': RegimeParams(grid_spacing=0.012, buy_levels=5, sell_levels=5, capital_pct=0.75, base_hold_pct=0.20),
            'BEAR': RegimeParams(grid_spacing=0.025, buy_levels=0, sell_levels=3, capital_pct=0.0, base_hold_pct=0.15),
        }

REGIME_PARAMS: Dict[str, RegimeParams] = _load_regime_params()

@dataclass
class GridLevel:
    price: float
    side: str
    qty: float
    size_usd: float
    order_id: Optional[str] = None
    status: str = 'pending'
    filled_at: Optional[str] = None
    linked_buy_price: Optional[float] = None

def is_weekend_window() -> bool:
    """
    Returns True during the low-liquidity weekend flush window:
    Friday 18:00 UTC through Sunday 18:00 UTC (including all of Saturday).
    """
    now_utc = datetime.now(timezone.utc)
    weekday = now_utc.weekday()  # 0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun
    hour = now_utc.hour
    if weekday == 4 and hour >= 18:       # Friday evening
        return True
    elif weekday == 5:                     # Saturday all day
        return True
    elif weekday == 6 and hour < 18:       # Sunday before 18:00 UTC
        return True
    return False


class GridEngine:

    def __init__(
        self,
        symbol: str,
        allocated_usd: float,
        paper_mode: bool = True,
        fee_rate: float = 0.001,
    ):
        self.symbol = symbol
        self.allocated_usd = allocated_usd
        self.paper_mode = paper_mode
        self.fee_rate = fee_rate
        
        self.current_regime: str = 'RANGE'
        self.params: RegimeParams = REGIME_PARAMS[self.current_regime]
        
        self.grid_levels: List[GridLevel] = []
        self.completed_cycles: List[Dict[str, Any]] = []
        self.exchange: Optional[ccxt.Exchange] = None
        self._last_rebuild_time: float = 0.0
        self.current_spacing: float = self.params.grid_spacing

        # Load optimizer-tuned params for this symbol if available
        self._apply_best_params()

    def _apply_best_params(self):
        """Read best_params.json written by auto_optimizer and apply to RANGE regime."""
        try:
            if not _BEST_PARAMS_FILE.exists():
                return
            best = json.loads(_BEST_PARAMS_FILE.read_text())
            sym_params = best.get(self.symbol)
            if not sym_params:
                return
            regime = sym_params.get('regime', 'RANGE')
            orig = REGIME_PARAMS.get(regime)
            if not orig:
                return
            REGIME_PARAMS[regime] = RegimeParams(
                grid_spacing=float(sym_params.get('grid_spacing', orig.grid_spacing)),
                buy_levels  =int(sym_params.get('buy_levels',    orig.buy_levels)),
                sell_levels =int(sym_params.get('sell_levels',   orig.sell_levels)),
                capital_pct =float(sym_params.get('capital_pct', orig.capital_pct)),
                base_hold_pct=orig.base_hold_pct,
            )
            logger.info(f"Loaded optimized params [{self.symbol} {regime}]: "
                        f"spacing={sym_params['grid_spacing']:.3f} "
                        f"buy={sym_params['buy_levels']} sell={sym_params['sell_levels']} "
                        f"cap={sym_params['capital_pct']:.0%}")
        except Exception as e:
            logger.debug(f"Could not load best_params for {self.symbol}: {e}")

    def set_regime(self, regime: str, exchange: Optional[ccxt.Exchange] = None):
        if regime not in REGIME_PARAMS:
            logger.error(f"Invalid regime {regime}")
            return
            
        old_params = self.params
        self.current_regime = regime
        self.params = REGIME_PARAMS[regime]
        
        if regime == 'BEAR':
            self.cancel_buys_only(exchange or self.exchange)
            logger.info(f"🛑 [{self.symbol}] Switched to BEAR regime (Price < SMA50, -DI dominant). Cancelled open buys to prevent catching falling knives.")

        if old_params and old_params.grid_spacing > 0:
            spacing_diff = abs(old_params.grid_spacing - self.params.grid_spacing) / old_params.grid_spacing
        else:
            spacing_diff = 1.0
        if spacing_diff > 0.30 or old_params.buy_levels != self.params.buy_levels:
            logger.info(f"Regime changed to {regime}. Spacing/levels changed > 30%. Forcing grid rebuild.")
            self._last_rebuild_time = 0.0

    def build_grid(self, current_price: float, portfolio=None, atr: float = 0.0, force: bool = False, dip_boost: float = 1.0):
        """Build grid levels. If ATR provided, use it for dynamic spacing. dip_boost scales deeper tiers."""
        if is_fee_buffer_asset(self.symbol) or is_never_sell_symbol(self.symbol):
            self.grid_levels = []
            logger.debug(f"[{self.symbol}] build_grid skipped — protected/fee-buffer asset (buy/hold only).")
            return
        if current_price <= 0:
            logger.warning(f"[{self.symbol}] Invalid current_price {current_price} in build_grid, skipping.")
            return
        import time
        now = time.time()

        # Throttle rebuilds: don't rebuild more than once per 10 minutes
        # unless grid is completely empty or force=True
        has_open_buys = any(l.status == 'open' and l.side == 'buy' for l in self.grid_levels)
        if not force and has_open_buys and (now - self._last_rebuild_time) < 600:
            return
        self._last_rebuild_time = now

        # If force=True, clear all pending/open levels to recalculate fresh fee-proof levels
        # Otherwise, preserve open SELL levels
        if force:
            self.grid_levels = [level for level in self.grid_levels if level.status == 'filled']
        else:
            self.grid_levels = [level for level in self.grid_levels
                                if level.status == 'filled'
                                or (level.status in ['open', 'pending'] and level.side == 'sell')]

        
        # ── Dynamic ATR-Expanded Spacing (Institutional Scaling) ──
        if atr > 0 and current_price > 0:
            atr_pct = atr / current_price
            # Scale grid spacing dynamically between 0.80% (quiet markets) and 2.50% (high-volatility flushes)
            dynamic_spacing = max(0.008, min(atr_pct * 0.90, 0.025))
            spacing = dynamic_spacing
        else:
            # Asset-specific ATR tuning groups fallback
            high_atr = {'ICP/USDT', 'ARB/USDT', 'NEAR/USDT', 'RENDER/USDT', 'FET/USDT', 'ATOM/USDT', 'SEI/USDT'}
            low_atr = {'SOL/USDT', 'AVAX/USDT', 'SUI/USDT'}
            if self.symbol in high_atr:
                spacing = 0.012  # 1.20% spacing for high-volatility pairs
            elif self.symbol in low_atr:
                spacing = 0.009  # 0.90% spacing for low-volatility pairs
            else:
                spacing = max(0.009, self.params.grid_spacing)
        
        # Base unit sizing per level for BUY orders (minimum $35.00 floor ensures each bounce yields >= +$0.50-$1.00 net)
        total_levels = self.params.buy_levels + self.params.sell_levels
        raw_order_size = (self.allocated_usd * self.params.capital_pct) / total_levels if total_levels > 0 else 0
        base_order_size = max(35.0, raw_order_size) if (raw_order_size > 0 and self.allocated_usd >= 35.0) else 0.0

        # 🛑 Position Exposure Hard Ceiling Guard inside build_grid
        if portfolio:
            tot_eq_val = float(getattr(portfolio, 'total_unified_equity', 0.0) or getattr(portfolio, 'total_capital', 0.0) or 0.0)
            if tot_eq_val > 0:
                h_pos = portfolio.get_position(self.symbol) if hasattr(portfolio, 'get_position') else 0.0
                h_val = h_pos * current_price
                if (h_val / tot_eq_val) >= 0.10:
                    base_order_size = 0.0
                    self.params.buy_levels = 0
                    self.allocated_usd = 0.0
                    logger.info(f"🛑 [{self.symbol}] Holding value (${h_val:,.2f}, {(h_val/tot_eq_val)*100:.1f}%) >= 10% equity limit. Suppressing all grid buy levels.")

        # 🛑 User Pause on ARB (Until After September 16, 2026 UTC):
        if is_arb_buy_paused(self.symbol):
            base_order_size = 0.0
            self.params.buy_levels = 0
            self.allocated_usd = 0.0
            logger.info(f"🛑 [{self.symbol}] User pause active until after September 16, 2026. Suppressing all grid buy levels.")

        # 🛑 Anti-Falling-Knife Guard: strictly suppress buys in confirmed BEAR regimes or when buy_levels is 0
        if self.current_regime == 'BEAR' or self.params.buy_levels <= 0 or self.allocated_usd < 35.0:
            base_order_size = 0.0
            if self.current_regime == 'BEAR':
                logger.info(f"🛑 [{self.symbol}] BEAR trend active (Price < SMA50, -DI dominant). Buying paused to prevent catching falling knives.")

        # Build BUY ladder (only if capital is allocated to this asset and not in BEAR regime)
        if base_order_size > 0 and self.current_regime != 'BEAR' and self.params.buy_levels > 0:
            # 🌊 Dip-Quality Buy Level Sizing:
            # When an intraday dip signal is detected (dip_boost > 1.05),
            # Level 1 is trimmed to 0.80x to conserve dry powder,
            # while deeper flush tiers (Levels 2-5) receive heavier conviction sizing
            # to accumulate heavier on exhaustion wicks and pull the cost basis down.
            if dip_boost > 1.05:
                boost_cap = min(1.35, dip_boost)
                tier_configs = [
                    (0.0078, 0.80),
                    (0.0145, 1.35 * boost_cap),
                    (0.0240, 1.75 * boost_cap),
                    (0.0380, 2.20 * boost_cap),
                    (0.0580, 2.70 * boost_cap),
                ]
            else:
                tier_configs = [
                    # (dip_pct, size_multiplier)
                    (0.0078, 1.00),  # Level 1: Rapid 0.78% dip -> targets +0.90% bounce (+$0.75 net / $100 fill)
                    (0.0145, 1.25),  # Level 2: 1.45% pullback -> targets +1.55% bounce (+$1.40 net / $100 fill)
                    (0.0240, 1.60),  # Level 3: 2.40% wave dip -> targets +2.50% bounce (+$2.35 net / $100 fill)
                    (0.0380, 2.00),  # Level 4: 3.80% flush dip -> targets +3.90% bounce (+$3.75 net / $100 fill)
                    (0.0580, 2.50),  # Level 5: 5.80% deep floor -> targets +5.90% bounce (+$5.75 net / $100 fill)
                ]

            buy_count = min(self.params.buy_levels, len(tier_configs))
            for i in range(buy_count):
                dip_pct, level_boost = tier_configs[i]
                
                # If BTC is in defensive BEAR mode, widen altcoin dip tiers
                try:
                    from trading_engine.spot.btc_master_filter import btc_master_filter
                    if self.symbol != 'BTC/USDT':
                        dip_pct *= btc_master_filter.get_spacing_multiplier()
                except Exception:
                    pass

                # If ATR is unusually elevated, scale Tier 2 & Tier 3 dynamically
                if atr > 0 and current_price > 0 and i >= 2:
                    atr_factor = max(1.0, min(1.4, (atr / current_price) / 0.015))
                    dip_pct = dip_pct * atr_factor

                price = current_price * (1.0 - dip_pct)
                lvl_size = base_order_size * level_boost
                qty = lvl_size / price
                self.grid_levels.append(GridLevel(
                    price=price,
                    side='buy',
                    qty=qty,
                    size_usd=lvl_size
                ))

        self.current_spacing = 0.0078




            
        if is_fee_buffer_asset(self.symbol) or is_never_sell_symbol(self.symbol):
            # Never build sell inventory for MNT / protected fee assets
            self.grid_levels = [lvl for lvl in self.grid_levels if lvl.side != 'sell']
            logger.debug(f"[{self.symbol}] Skipping sell-grid build (protected/fee-buffer asset).")
            return

        # Build Sell Levels (strictly above average purchase cost basis)
        def _get_val(obj, key, default=0.0):
            if obj is None:
                return default
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        holding = None
        if portfolio and hasattr(portfolio, 'holdings') and isinstance(portfolio.holdings, dict):
            holding = portfolio.holdings.get(self.symbol)
            if not holding:
                base_asset = self.symbol.split('/')[0]
                for sym_key, h in portfolio.holdings.items():
                    if sym_key == base_asset or sym_key.startswith(f"{base_asset}/") or sym_key == self.symbol:
                        holding = h
                        break

        base_qty_held = float(_get_val(holding, 'units_held', 0.0) or 0.0)
        avg_cost = float(_get_val(holding, 'avg_cost_basis', 0.0) or 0.0)

        # Query authoritative SQLite FIFO inventory to get the exact un-exited buy price
        fifo_max_cost = 0.0
        fifo_basis = {}
        oldest_lot_age_hours = 0.0
        try:
            from trading_engine.spot.fifo_reconciler import get_fifo_cost_basis
            fifo_basis = get_fifo_cost_basis(self.symbol, units_held=base_qty_held if base_qty_held > 0 else None) or {}
            if fifo_basis.get('avg_cost', 0) > 0:
                fifo_max_cost = float(fifo_basis.get('max_buy_price', 0.0) or 0.0)
                # Use live FIFO average as portfolio floor. Per-sell floors use
                # get_fifo_lot_cost_for_qty (oldest lots first) via resolve_sell_cost_ref,
                # so one expensive lot no longer pins every sell above the whole book.
                avg_cost = max(avg_cost, float(fifo_basis['avg_cost']))
            oldest_lot_age_hours = float(fifo_basis.get('oldest_buy_age_hours', 0.0) or 0.0)
        except Exception as e_fifo_cb:
            logger.debug(f"FIFO cost basis lookup for {self.symbol}: {e_fifo_cb}")
            fifo_basis = {}

        is_aged_position = (oldest_lot_age_hours >= 12.0)  # recycle capital sooner while still fee-proof

        # Safety: If avg_cost is 0 or missing, ensure we never sell below current_price * 1.015
        if avg_cost <= 0:
            avg_cost = current_price

        if base_qty_held > 0.000001:
            total_held_usd = base_qty_held * current_price
            if total_held_usd < 5.0:
                # Sub-$5 dust cannot be placed as a Bybit limit order
                return
            from trading_engine.config import spot_settings
            is_legacy = self.symbol not in spot_settings.asset_list

            # FIFO age is often 0 on Railway when fill timestamps are missing. Legacy bags
            # should still use compressed/quick-exit geometry so capital is not parked.
            if (not is_aged_position) and is_legacy and oldest_lot_age_hours <= 0:
                is_aged_position = True
                logger.info(
                    f"⏳ [{self.symbol}] FIFO lot age unavailable — treating legacy holding as aged "
                    f"for compressed/quick-exit sells (units={base_qty_held:.4f})."
                )

            if is_legacy:
                # 🚪 Quick-Exit Mode for Legacy Holdings Outside Top 12:
                # Consolidate holding into 1 order (or 2 if > $150) with tight target price to guarantee
                # > $0.50 net profit (+$0.75 target) and exit into liquid USDT cash immediately.
                fee_factor = get_fee_factor()
                sell_levels_count = 1 if total_held_usd < 150.0 else 2
                qty_per_sell = base_qty_held / sell_levels_count
                min_net_usd = 0.75  # Target guaranteed > $0.50 net profit

                for i in range(sell_levels_count):
                    from trading_engine.spot.runner import ALL_23_HISTORICAL_COSTS
                    hist_cost = float(ALL_23_HISTORICAL_COSTS.get(self.symbol, 0.0) or 0.0)
                    # Live FIFO / portfolio cost first; hist only if live unknown (avoids stale floors stalling cycles)
                    cost_ref = resolve_sell_cost_ref(
                        self.symbol,
                        portfolio_avg_cost=avg_cost,
                        units_held=base_qty_held,
                        sell_qty=qty_per_sell,
                        hist_cost=hist_cost,
                        current_price=current_price,
                    )
                    if cost_ref <= 0:
                        cost_ref = current_price
                    denom = qty_per_sell * (1.0 - fee_factor)
                    min_fee_proof_price = (cost_ref * qty_per_sell * (1.0 + fee_factor) + min_net_usd) / denom if denom > 0 else cost_ref * 1.005

                    if current_price > cost_ref:
                        # Already in profit: place slightly above market for instant execution (+0.35% + 0.20%*i)
                        target_p = max(min_fee_proof_price, current_price * (1.0035 + 0.0020 * i))
                    else:
                        # Underwater: place at the exact minimum price to break even + $0.75 profit
                        target_p = max(min_fee_proof_price, min_fee_proof_price * (1.0 + 0.0030 * i))

                    # Absolute Hard Safety Gate: target_p MUST be strictly >= min_fee_proof_price
                    target_p = max(target_p, min_fee_proof_price)

                    # Post-only safety: if target_p <= current_price, current price is so high that target_p would cross spread,
                    # so place slightly above current_price while still strictly preserving profit
                    if target_p <= current_price:
                        target_p = max(min_fee_proof_price, current_price * 1.0035)

                    actual_net_pnl = (target_p * qty_per_sell * (1.0 - fee_factor)) - (cost_ref * qty_per_sell * (1.0 + fee_factor))
                    if actual_net_pnl < 0.60:
                        target_p = (cost_ref * qty_per_sell * (1.0 + fee_factor) + 0.60) / denom if denom > 0 else target_p
                        actual_net_pnl = (target_p * qty_per_sell * (1.0 - fee_factor)) - (cost_ref * qty_per_sell * (1.0 + fee_factor))

                    # Final failsafe: if for any floating-point reason target_p is still below cost_ref, abort level!
                    if target_p < cost_ref:
                        logger.critical(f"🚨 [{self.symbol}] FATAL: target_p (${target_p:.4f}) < cost_ref (${cost_ref:.4f})! Aborting sell level to prevent loss!")
                        continue
                    if not sell_is_fee_proof(target_p, cost_ref, qty_per_sell, fee_factor=fee_factor, min_net_usd=0.55):
                        target_p = enforce_sell_floor(target_p, cost_ref, qty_per_sell, fee_factor=fee_factor, min_net_usd=0.60)
                        actual_net_pnl = estimated_net_pnl(target_p, cost_ref, qty_per_sell, fee_factor=fee_factor)
                        if not sell_is_fee_proof(target_p, cost_ref, qty_per_sell, fee_factor=fee_factor, min_net_usd=0.55):
                            logger.critical(f"🚨 [{self.symbol}] FATAL: cannot build fee-proof sell (target ${target_p:.4f}, cost ${cost_ref:.4f}). Aborting level!")
                            continue

                    logger.info(f"🚪 [{self.symbol}] Quick-Exit Sell Level {i+1}/{sell_levels_count}: Target=${target_p:.4f} "
                                f"(CostRef: ${cost_ref:.4f}, Live: ${current_price:.4f}, Net Profit: +${actual_net_pnl:.2f} USD)")

                    self.grid_levels.append(GridLevel(
                        price=target_p,
                        side='sell',
                        qty=qty_per_sell,
                        size_usd=qty_per_sell * target_p,
                        linked_buy_price=cost_ref
                    ))
            else:
                # Dynamic Tier Consolidation:
                # If total holding is < $60 USD, use 1 SINGLE order (e.g. all 287 ALGO in 1 order) to avoid tiny micro-orders
                # If total holding is $60 - $120 USD, use 2 orders (~$50 each)
                # If total holding is > $120 USD, use up to 4 orders (~$30-$300 each)
                if total_held_usd < 60.0:
                    sell_levels_count = 1
                elif total_held_usd < 120.0:
                    sell_levels_count = 2
                else:
                    sell_levels_count = max(1, min(self.params.sell_levels, 4))

                qty_per_sell = base_qty_held / sell_levels_count

                # Tiered net profit guarantee:
                min_net_profit_tiers = [0.60, 0.90, 1.30, 2.00]
                fee_factor = get_fee_factor()  # Bybit per-side fee + safety buffer

                for i in range(sell_levels_count):
                    min_net_usd = min_net_profit_tiers[i] if i < len(min_net_profit_tiers) else (0.60 + 0.40 * i)
                    if is_aged_position:
                        # ⚡ Age-Weighted Target Compression: For holdings > 24h old,
                        # collapse target to fee-proof profit ($0.60) across all tiers to recycle capital rapidly
                        min_net_usd = 0.60
                    else:
                        min_net_usd = max(0.60, min_net_usd)
                    
                    from trading_engine.spot.runner import ALL_23_HISTORICAL_COSTS
                    hist_cost = float(ALL_23_HISTORICAL_COSTS.get(self.symbol, 0.0) or 0.0)
                    # Live FIFO / portfolio cost first; hist only if live unknown (avoids stale floors stalling cycles)
                    cost_ref = resolve_sell_cost_ref(
                        self.symbol,
                        portfolio_avg_cost=avg_cost,
                        units_held=base_qty_held,
                        sell_qty=qty_per_sell,
                        hist_cost=hist_cost,
                        current_price=current_price,
                    )
                    if cost_ref <= 0.0:
                        cost_ref = current_price

                    denom = qty_per_sell * (1.0 - fee_factor)
                    # Minimum price needed to guarantee at least min_net_usd profit after 2-sided fees:
                    min_fee_proof_price = (cost_ref * qty_per_sell * (1.0 + fee_factor) + min_net_usd) / denom if denom > 0 else cost_ref * 1.015

                    if is_aged_position:
                        # Stagnant position (>24h): tightest possible fee-proof profit exit (+0.0%, +0.1%, +0.2% stagger)
                        stagger_step = 0.0010 * i
                        target_p = max(min_fee_proof_price, min_fee_proof_price * (1.0 + stagger_step))
                    elif current_price > cost_ref:
                        tight_spread = 0.0035 + (0.0025 * i)  # Tier 1: +0.35%, Tier 2: +0.60%, Tier 3: +0.85%, Tier 4: +1.10%
                        tight_market_target = current_price * (1.0 + tight_spread)
                        target_p = max(min_fee_proof_price, tight_market_target)
                    else:
                        # If current market price is below cost basis (underwater), order sits safely above cost_ref + stagger
                        stagger_step = 0.0040 * i
                        target_p = max(min_fee_proof_price, min_fee_proof_price * (1.0 + stagger_step))

                    # Absolute Hard Safety Gate: target_p MUST be strictly >= min_fee_proof_price
                    target_p = max(target_p, min_fee_proof_price)

                    # Ensure post-only safety: target_p must be strictly above current_price
                    if target_p <= current_price:
                        target_p = max(min_fee_proof_price, current_price * 1.0040)
                    
                    # Double check net profit calculation to guarantee >= $0.55 USD
                    actual_net_pnl = (target_p * qty_per_sell * (1.0 - fee_factor)) - (cost_ref * qty_per_sell * (1.0 + fee_factor))
                    if actual_net_pnl < 0.55:
                        target_p = (cost_ref * qty_per_sell * (1.0 + fee_factor) + 0.60) / denom if denom > 0 else target_p
                        actual_net_pnl = (target_p * qty_per_sell * (1.0 - fee_factor)) - (cost_ref * qty_per_sell * (1.0 + fee_factor))

                    # Final failsafe: if target_p < cost_ref, abort level!
                    if target_p < cost_ref:
                        logger.critical(f"🚨 [{self.symbol}] FATAL: target_p (${target_p:.4f}) < cost_ref (${cost_ref:.4f})! Aborting sell level to prevent loss!")
                        continue
                    if not sell_is_fee_proof(target_p, cost_ref, qty_per_sell, fee_factor=fee_factor, min_net_usd=0.55):
                        target_p = enforce_sell_floor(target_p, cost_ref, qty_per_sell, fee_factor=fee_factor, min_net_usd=0.60)
                        actual_net_pnl = estimated_net_pnl(target_p, cost_ref, qty_per_sell, fee_factor=fee_factor)
                        if not sell_is_fee_proof(target_p, cost_ref, qty_per_sell, fee_factor=fee_factor, min_net_usd=0.55):
                            logger.critical(f"🚨 [{self.symbol}] FATAL: cannot build fee-proof sell (target ${target_p:.4f}, cost ${cost_ref:.4f}). Aborting level!")
                            continue

                    tier_label = "Compressed Aged" if is_aged_position else "High-Velocity"
                    logger.info(f"🎯 [{self.symbol}] {tier_label} Sell Level {i+1}/{sell_levels_count}: Target=${target_p:.4f} "
                                f"(CostRef: ${cost_ref:.4f}, Live: ${current_price:.4f}, Age: {oldest_lot_age_hours:.1f}h, Net Profit: +${actual_net_pnl:.2f} USD)")

                    self.grid_levels.append(GridLevel(
                        price=target_p,
                        side='sell',
                        qty=qty_per_sell,
                        size_usd=qty_per_sell * target_p,
                        linked_buy_price=cost_ref
                    ))









    def _refuse_protected_sell(self) -> bool:
        """True if this symbol must never place/record a sell (MNT fee buffer, etc.)."""
        if is_never_sell_symbol(self.symbol) or is_fee_buffer_asset(self.symbol):
            logger.critical(f"🚫 [{self.symbol}] Refusing sell — protected fee-buffer / never-sell asset.")
            return True
        return False

    def place_grid_orders(self, portfolio, exchange: ccxt.Exchange):
        self.exchange = exchange
        for level in self.grid_levels:
            if level.status == 'pending':
                if is_never_sell_symbol(self.symbol) and level.side == 'sell':
                    logger.critical(
                        f"🚫 [{self.symbol}] BLOCKED sell place — fee-buffer/protected asset must never be sold."
                    )
                    level.status = 'cancelled'
                    continue
                if is_fee_buffer_asset(self.symbol) or is_never_buy_symbol(self.symbol):
                    logger.warning(f"🚫 [{self.symbol}] Skipping grid place — MNT fee-buffer is hold-only (no buy/sell).")
                    level.status = 'cancelled'
                    continue

                if self.paper_mode:
                    level.status = 'open'
                    level.order_id = f'PAPER_{uuid4().hex[:8]}'
                    logger.info(f"[PAPER] Placed {level.side} limit order for {self.symbol} at {level.price} (Qty: {level.qty})")
                else:
                    try:
                        qty_val = float(exchange.amount_to_precision(self.symbol, level.qty)) if hasattr(exchange, 'amount_to_precision') else level.qty
                        price_val = float(exchange.price_to_precision(self.symbol, level.price)) if hasattr(exchange, 'price_to_precision') else level.price
                        
                        if hasattr(exchange, 'market') and self.symbol in exchange.markets:
                            prec = exchange.market(self.symbol).get('precision', {}).get('amount')
                            if isinstance(prec, (float, int)) and float(prec) > 0:
                                decimals = max(0, -int(math.floor(math.log10(float(prec)))))
                                if level.side == 'sell':
                                    qty_val = math.floor(qty_val * (10 ** decimals)) / (10 ** decimals)
                                else:
                                    qty_val = round(qty_val, decimals)

                            prec_p = exchange.market(self.symbol).get('precision', {}).get('price')
                            if isinstance(prec_p, (float, int)) and float(prec_p) > 0:
                                decimals_p = max(0, -int(math.floor(math.log10(float(prec_p)))))
                                price_val = round(price_val, decimals_p)
                        
                        # Enforce minimum amount and minimum notional cost limit (Bybit requires min $5.00 per spot order)
                        if hasattr(exchange, 'market') and self.symbol in exchange.markets:
                            m_info = exchange.market(self.symbol)
                            min_amt = m_info.get('limits', {}).get('amount', {}).get('min', None)
                            min_cost = m_info.get('limits', {}).get('cost', {}).get('min', 5.0) or 5.0
                            if min_amt is not None and qty_val < float(min_amt):
                                qty_val = float(min_amt)
                            # Hard floor: every buy order must be >= $35 USD to guarantee >= +$0.50 net profit per fill
                            required_cost = max(35.0, float(min_cost))
                            if price_val > 0 and (qty_val * price_val) < required_cost and level.side == 'buy':
                                qty_val = required_cost / price_val
                                if hasattr(exchange, 'amount_to_precision'):
                                    qty_val = float(exchange.amount_to_precision(self.symbol, qty_val))
                                    prec = exchange.market(self.symbol).get('precision', {}).get('amount')
                                    if isinstance(prec, (float, int)) and float(prec) > 0:
                                        decimals = max(0, -int(math.floor(math.log10(float(prec)))))
                                        qty_val = round(qty_val, decimals)
                            elif price_val > 0 and (qty_val * price_val) < float(min_cost) and level.side == 'sell':
                                logger.debug(f"[{self.symbol}] Skipping sub-$5 sell order (${qty_val * price_val:.2f} < ${min_cost:.2f}) until consolidated.")
                                continue

                        if level.side == 'buy':
                            try:
                                from trading_engine.spot.btc_master_filter import btc_master_filter
                                is_btc_safe, btc_reason = btc_master_filter.is_safe_for_alt_buys(self.symbol)
                                if not is_btc_safe:
                                    logger.debug(f"[{self.symbol}] 🛑 [BTC GUARD] Pausing buy order: {btc_reason}")
                                    continue
                            except Exception as e_btc:
                                logger.debug(f"BTC filter check error: {e_btc}")

                        if level.side == 'buy' and not self.paper_mode and exchange:
                            res_floor = float(getattr(portfolio, 'usdt_reserved', 0.0) or 0.0)
                            avail_usdt = float(getattr(portfolio, 'usdt_available', 0.0) or 0.0)
                            req_cost = qty_val * price_val
                            open_buys_usd = float(getattr(portfolio, 'total_open_buy_usd', 0.0) or 0.0)

                            # 🛑 AIRTIGHT HARD FLOOR RULE:
                            # (Total USDT - All Open Buy Commitments - New Order Cost) MUST be >= Reserve Floor
                            if res_floor > 0 and (avail_usdt - open_buys_usd - req_cost) < res_floor:
                                logger.debug(f"[{self.symbol}] Pausing buy order (${req_cost:.2f}) - Total open buy commitments (${open_buys_usd:.2f}) would breach cash reserve (${res_floor:,.2f} floor).")
                                level.status = 'cancelled'
                                continue

                            # 🛑 STRICT 10% MAXIMUM ASSET EXPOSURE CEILING:
                            # (Current Holding Value + Existing Open Buys + New Buy Order) MUST NOT exceed 10% of Total Unified Equity
                            tot_eq_val = float(getattr(portfolio, 'total_unified_equity', 0.0) or getattr(portfolio, 'total_capital', 0.0) or 0.0)
                            if tot_eq_val > 0:
                                cap_10 = tot_eq_val * 0.10
                                h_qty = portfolio.get_position(self.symbol) if hasattr(portfolio, 'get_position') else 0.0
                                h_val = h_qty * price_val
                                existing_open_buys = sum(float(getattr(l, 'qty', 0) or 0) * float(getattr(l, 'price', 0) or 0) for l in self.grid_levels if getattr(l, 'status', '') == 'open' and getattr(l, 'side', '') == 'buy')
                                if (h_val + existing_open_buys + req_cost) > cap_10:
                                    logger.info(f"[{self.symbol}] 🛑 [10% CEILING] Skipping buy order (${req_cost:.2f}) - Total exposure (${(h_val + existing_open_buys + req_cost):.2f}) would exceed 10% equity cap (${cap_10:.2f}).")
                                    level.status = 'cancelled'
                                    continue

                            # 🛑 Temporary User Pause on ARB (Until After September 16, 2026 UTC):
                            if is_arb_buy_paused(self.symbol):
                                logger.info(f"[{self.symbol}] 🛑 [PAUSE TILL SEPT 24] Skipping buy order (${req_cost:.2f}) - paused until after September 23, 2026.")
                                level.status = 'cancelled'
                                continue

                        # Placement-time never-sell-below-buy gate (after exchange precision rounding)
                        if level.side == 'sell':
                            cost_floor = float(getattr(level, 'linked_buy_price', 0.0) or 0.0)
                            if cost_floor <= 0:
                                try:
                                    cost_floor = resolve_sell_cost_ref(
                                        self.symbol,
                                        sell_qty=qty_val,
                                        current_price=price_val,
                                    )
                                except Exception:
                                    cost_floor = 0.0
                            if cost_floor > 0 and not sell_clears_buy(price_val, cost_floor):
                                safe_p = enforce_sell_floor(price_val, cost_floor, qty_val, fee_factor=get_fee_factor(), min_net_usd=0.60)
                                logger.warning(
                                    f"🛡️ [{self.symbol}] Bumping sell before place: "
                                    f"${price_val:.6f} -> ${safe_p:.6f} (cost floor ${cost_floor:.6f})"
                                )
                                price_val = float(exchange.price_to_precision(self.symbol, safe_p)) if hasattr(exchange, 'price_to_precision') else safe_p
                                if not sell_clears_buy(price_val, cost_floor):
                                    logger.critical(
                                        f"🚨 [{self.symbol}] Aborting sell place: rounded ${price_val:.6f} still < cost ${cost_floor:.6f}"
                                    )
                                    level.status = 'cancelled'
                                    continue
                                level.price = price_val
                                level.linked_buy_price = cost_floor

                        params = {'category': 'spot', 'postOnly': True} if 'bybit' in str(type(exchange)).lower() else {}
                        try:
                            if level.side == 'buy':
                                order = exchange.create_limit_buy_order(self.symbol, qty_val, price_val, params)
                            else:
                                if self._refuse_protected_sell():
                                    order = None
                                else:
                                    order = exchange.create_limit_sell_order(self.symbol, qty_val, price_val, params)
                        except Exception as e_post:
                            # If postOnly was rejected because price is at or across spread, retry with standard limit order
                            if 'postonly' in str(e_post).lower() or 'post_only' in str(e_post).lower() or '170193' in str(e_post):
                                fallback_params = {'category': 'spot'} if 'bybit' in str(type(exchange)).lower() else {}
                                if level.side == 'buy':
                                    order = exchange.create_limit_buy_order(self.symbol, qty_val, price_val, fallback_params)
                                else:
                                    if self._refuse_protected_sell():
                                        order = None
                                    else:
                                        order = exchange.create_limit_sell_order(self.symbol, qty_val, price_val, fallback_params)
                            elif level.side == 'sell' and ('balance' in str(e_post).lower() or '170131' in str(e_post)):
                                # Insufficient balance on sell: fetch exact available coin balance and retry
                                try:
                                    base_coin = self.symbol.split('/')[0]
                                    c_bal = exchange.fetch_balance({'accountType': 'UNIFIED'})
                                    free_coin = float(c_bal.get('free', {}).get(base_coin, 0.0) or 0.0)
                                    if free_coin > 0:
                                        new_qty = float(exchange.amount_to_precision(self.symbol, free_coin)) if hasattr(exchange, 'amount_to_precision') else free_coin
                                        prec = exchange.market(self.symbol).get('precision', {}).get('amount') if hasattr(exchange, 'market') and self.symbol in exchange.markets else None
                                        if isinstance(prec, (float, int)) and float(prec) > 0:
                                            decimals = max(0, -int(math.floor(math.log10(float(prec)))))
                                            new_qty = math.floor(new_qty * (10 ** decimals)) / (10 ** decimals)
                                        if new_qty > 0 and (new_qty * price_val) >= float(min_cost if 'min_cost' in locals() else 5.0):
                                            logger.info(f"[{self.symbol}] Adjusting sell qty from {qty_val} to exact exchange balance {new_qty}")
                                            if self._refuse_protected_sell():
                                                order = None
                                            else:
                                                order = exchange.create_limit_sell_order(self.symbol, new_qty, price_val, {'category': 'spot'})
                                            qty_val = new_qty
                                        else:
                                            raise e_post
                                    else:
                                        raise e_post
                                except Exception:
                                    raise e_post
                            else:
                                raise e_post

                        level.status = 'open'
                        level.order_id = order['id']
                        if level.side == 'buy' and hasattr(portfolio, 'total_open_buy_usd'):
                            portfolio.total_open_buy_usd = float(getattr(portfolio, 'total_open_buy_usd', 0.0)) + req_cost
                        logger.info(f"[LIVE] Placed {level.side} limit order for {self.symbol} at {price_val} (Qty: {qty_val}, ID: {order['id']})")


                    except Exception as e:
                        logger.error(f"Failed to place {level.side} order for {self.symbol}: {e}")


    def tick(self, current_price: float, portfolio, exchange=None, open_orders_by_id=None) -> List[Dict[str, Any]]:
        """Check grid levels against current price and simulate/process fills."""
        fills = []
        for level in list(self.grid_levels):
            if level.status != 'open':
                continue
                
            is_filled = False
            if not self.paper_mode and exchange and level.order_id and not level.order_id.startswith('PAPER_'):
                # Fast bypass: if open orders were fetched from Bybit in bulk and this order is still on the book, it is not filled
                if open_orders_by_id is not None and str(level.order_id) in open_orders_by_id:
                    continue

                try:
                    order_info = exchange.fetch_order(level.order_id, self.symbol, {'category': 'spot', 'acknowledged': True})
                    st = (order_info.get('status') or '').lower()
                    if st in ['closed', 'filled']:
                        is_filled = True
                    elif st in ['canceled', 'cancelled', 'rejected']:
                        level.status = 'cancelled'
                        continue
                except Exception as e_ord:
                    logger.debug(f"Fetch live order status failed for {level.order_id}: {e_ord}")
                    continue
            else:
                if level.side == 'buy' and current_price <= level.price:
                    is_filled = True
                elif level.side == 'sell' and current_price >= level.price:
                    is_filled = True
                
            if is_filled:
                level.status = 'filled'
                level.filled_at = datetime.now(timezone.utc).isoformat()
                
                # If running live on exchange, attempt to clean up original order from exchange orderbook
                if exchange and level.order_id and not level.order_id.startswith('PAPER_'):
                    try:
                        exchange.cancel_order(level.order_id, self.symbol, {'category': 'spot'})
                    except Exception:
                        pass

                if level.side == 'buy':
                    # Guaranteed Profit Floor: Replacement sell order MUST yield at least +$0.60 NET cash after fees
                    min_net_usd = 0.60
                    fee_factor = get_fee_factor()
                    # Linked to THIS buy fill — never inflate with unrelated higher lots
                    cost_ref = resolve_sell_cost_ref(
                        self.symbol,
                        linked_buy_price=level.price,
                        sell_qty=level.qty,
                        current_price=level.price,
                    )
                    if cost_ref <= 0:
                        cost_ref = level.price
                    sell_price = enforce_sell_floor(
                        max(cost_ref * 1.0090, level.price * 1.0090),
                        cost_ref,
                        level.qty,
                        fee_factor=fee_factor,
                        min_net_usd=min_net_usd,
                    )

                    new_sell = GridLevel(
                        price=sell_price,
                        side='sell',
                        qty=level.qty,
                        size_usd=sell_price * level.qty,
                        linked_buy_price=cost_ref
                    )
                    self.grid_levels.append(new_sell)
                    if self.paper_mode:
                        new_sell.status = 'open'
                        new_sell.order_id = f'PAPER_{uuid4().hex[:8]}'
                    fills.append({'side': 'buy', 'price': level.price, 'qty': level.qty})
                    logger.info(f"BUY filled at {level.price}. Created new SELL level at {sell_price:.4f} (CostRef: ${cost_ref:.4f}, Guaranteed Net: +${min_net_usd:.2f})")

                    
                elif level.side == 'sell':
                    if is_never_sell_symbol(self.symbol):
                        logger.critical(f"🚫 [{self.symbol}] BLOCKED sell fill handling — protected asset.")
                        level.status = 'cancelled'
                        continue
                    buy_orig_p = level.linked_buy_price or (level.price / (1 + getattr(self, 'current_spacing', self.params.grid_spacing)))

                    # ── HARD GATE: never sell below buy, and require >= $0.50 net after fees ──
                    MIN_NET_PROFIT_USD = 0.50
                    if buy_orig_p > 0 and not sell_clears_buy(level.price, buy_orig_p):
                        fee_factor = self.fee_rate
                        denom = level.qty * (1.0 - fee_factor) if level.qty > 0 else 0.0
                        min_sell_price = enforce_sell_floor(
                            buy_orig_p * 1.0090,
                            buy_orig_p,
                            level.qty,
                            fee_factor=fee_factor,
                            min_net_usd=MIN_NET_PROFIT_USD,
                        )
                        logger.warning(
                            f"⛔ [{self.symbol}] BLOCKED sell below buy: "
                            f"Sell @ ${level.price:.4f} < Cost @ ${buy_orig_p:.4f}. "
                            f"Replacing with floor sell @ ${min_sell_price:.4f}"
                        )
                        level.status = 'open'
                        if exchange and level.order_id and not str(level.order_id).startswith('PAPER'):
                            try:
                                exchange.cancel_order(level.order_id, self.symbol, {'category': 'spot'})
                                new_params = {'category': 'spot', 'postOnly': True}
                                qty_val = float(exchange.amount_to_precision(self.symbol, level.qty)) if hasattr(exchange, 'amount_to_precision') else level.qty
                                p_val = float(exchange.price_to_precision(self.symbol, min_sell_price)) if hasattr(exchange, 'price_to_precision') else min_sell_price
                                if not sell_clears_buy(p_val, buy_orig_p):
                                    p_val = float(exchange.price_to_precision(self.symbol, buy_orig_p * 1.01)) if hasattr(exchange, 'price_to_precision') else (buy_orig_p * 1.01)
                                if self._refuse_protected_sell():
                                    new_order = None
                                else:
                                    new_order = exchange.create_limit_sell_order(self.symbol, qty_val, p_val, new_params)
                                level.price = float(p_val)
                                level.linked_buy_price = buy_orig_p
                                level.order_id = new_order['id']
                                logger.info(f"✅ [{self.symbol}] Replaced below-buy sell @ ${level.price:.4f}")
                            except Exception as e_rep:
                                logger.error(f"Failed to replace below-buy sell for {self.symbol}: {e_rep}")
                        continue

                    fee_factor = get_fee_factor()
                    gross_pnl = (level.price - buy_orig_p) * level.qty
                    fee = (level.price * level.qty * fee_factor) + (buy_orig_p * level.qty * fee_factor)
                    net_pnl = estimated_net_pnl(level.price, buy_orig_p, level.qty, fee_factor=fee_factor)

                    if (net_pnl < MIN_NET_PROFIT_USD or not sell_is_fee_proof(level.price, buy_orig_p, level.qty, fee_factor=fee_factor, min_net_usd=MIN_NET_PROFIT_USD)) and level.qty > 0 and buy_orig_p > 0:
                        # Bybit fees must not eat the edge into a loss — bump to fee-proof floor
                        min_sell_price = enforce_sell_floor(
                            level.price,
                            buy_orig_p,
                            level.qty,
                            fee_factor=fee_factor,
                            min_net_usd=MIN_NET_PROFIT_USD,
                        )
                        logger.warning(
                            f"⛔ [{self.symbol}] BLOCKED fee-unsafe sell fill: would have netted ${net_pnl:.4f} after Bybit fees "
                            f"(Sell @ ${level.price:.4f}, Cost @ ${buy_orig_p:.4f}, Qty: {level.qty:.6f}). "
                            f"Replacing with fee-proof sell @ ${min_sell_price:.4f}"
                        )
                        level.status = 'open'  # Revert status so it doesn't get counted as filled
                        if exchange and level.order_id and not str(level.order_id).startswith('PAPER'):
                            try:
                                exchange.cancel_order(level.order_id, self.symbol, {'category': 'spot'})
                                new_params = {'category': 'spot', 'postOnly': True}
                                qty_val = float(exchange.amount_to_precision(self.symbol, level.qty)) if hasattr(exchange, 'amount_to_precision') else level.qty
                                p_val = float(exchange.price_to_precision(self.symbol, min_sell_price)) if hasattr(exchange, 'price_to_precision') else min_sell_price
                                if self._refuse_protected_sell():
                                    new_order = None
                                else:
                                    new_order = exchange.create_limit_sell_order(self.symbol, qty_val, p_val, new_params)
                                level.price = min_sell_price
                                level.order_id = new_order['id']
                                logger.info(f"✅ [{self.symbol}] Replaced with guaranteed sell @ ${min_sell_price:.4f} (Net >= +${MIN_NET_PROFIT_USD:.2f} USD)")
                            except Exception as e_rep:
                                logger.error(f"Failed to replace sub-$0.50 sell for {self.symbol}: {e_rep}")
                        continue  # Skip recording this as a completed cycle

                    if hasattr(portfolio, 'record_sell'):
                        portfolio.record_sell(self.symbol, level.qty, level.price, level.size_usd, level.order_id or '', buy_orig_p)

                    cycle_record = {
                        'symbol': self.symbol,
                        'buy_order_id': getattr(level, 'linked_order_id', ''),
                        'sell_order_id': level.order_id or '',
                        'buy_price': buy_orig_p,
                        'sell_price': level.price,
                        'qty': level.qty,
                        'gross_pnl': gross_pnl,
                        'fee': fee,
                        'net_pnl': net_pnl,
                        'timestamp': level.filled_at or datetime.now(timezone.utc).isoformat()
                    }
                    self.completed_cycles.append(cycle_record)
                    if not self.paper_mode and level.order_id and not str(level.order_id).startswith('PAPER'):
                        try:
                            from trading_engine.spot.trade_db import record_completed_cycle
                            record_completed_cycle(cycle_record)
                        except Exception as e_db:
                            logger.debug(f"Failed to record cycle to SQLite: {e_db}")
                    logger.info(f"✅ SELL filled at {level.price}. Completed cycle for {self.symbol}. Net PnL: +${net_pnl:.2f} USD")





                
                    from trading_engine.config import spot_settings
                    if (self.symbol in spot_settings.asset_list and 
                        getattr(self, 'allocated_usd', 0.0) > 0 and 
                        self.current_regime != 'BEAR' and 
                        getattr(self.params, 'buy_levels', 0) > 0 and
                        not is_arb_buy_paused(self.symbol)):
                        # Re-enter at the completed cycle's buy price (grid dip), same qty — keeps cycle velocity high
                        reentry_price = buy_orig_p if buy_orig_p > 0 else level.price
                        reentry_qty = level.qty if level.qty > 0 else (level.size_usd / reentry_price if reentry_price > 0 else 0.0)
                        new_buy = GridLevel(
                            price=reentry_price,
                            side='buy',
                            qty=reentry_qty,
                            size_usd=reentry_price * reentry_qty
                        )
                        self.grid_levels.append(new_buy)
                        if self.paper_mode:
                            new_buy.status = 'open'
                            new_buy.order_id = f'PAPER_{uuid4().hex[:8]}'
                    else:
                        logger.info(f"🎉 [{self.symbol}] Sell-Only/BEAR position exited into liquid USDT cash. No replacement buy placed.")
                        
                    fills.append({'side': 'sell', 'price': level.price, 'qty': level.qty})
                    
        return fills

    def cancel_all(self, exchange: Optional[ccxt.Exchange] = None):
        exch = exchange or self.exchange
        if not self.paper_mode and exch:
            try:
                params = {'category': 'spot'} if 'bybit' in str(type(exch)).lower() else {}
                if hasattr(exch, 'cancel_all_orders'):
                    exch.cancel_all_orders(self.symbol, params=params)
                    logger.info(f"[LIVE] Bulk cancelled all open orders for {self.symbol} on Bybit")
            except Exception as e_bulk:
                logger.debug(f"Bulk cancel for {self.symbol}: {e_bulk}")

        for level in self.grid_levels:
            if level.status in ['open', 'pending']:
                if self.paper_mode:
                    level.status = 'cancelled'
                    logger.info(f"[PAPER] Cancelled order {level.order_id}")
                else:
                    level.status = 'cancelled'
                    if exch and level.order_id:
                        try:
                            exch.cancel_order(level.order_id, self.symbol)
                        except Exception:
                            pass

    def cancel_buys_only(self, exchange: Optional[ccxt.Exchange] = None):
        """
        Cancel ONLY open buy orders on this asset to free active capital,
        leaving all resting take-profit SELL limit orders completely intact!
        """
        exch = exchange or self.exchange
        for level in self.grid_levels:
            if level.status in ['open', 'pending'] and level.side == 'buy':
                if self.paper_mode:
                    level.status = 'cancelled'
                    logger.info(f"[PAPER] Cancelled buy order {level.order_id} on {self.symbol}")
                else:
                    level.status = 'cancelled'
                    if exch and level.order_id:
                        try:
                            exch.cancel_order(level.order_id, self.symbol)
                            logger.info(f"[LIVE] Cancelled demoted buy order {level.order_id} on {self.symbol}")
                        except Exception as e_c:
                            logger.debug(f"Could not cancel buy order {level.order_id} on {self.symbol}: {e_c}")
        # Retain only sell levels in memory
        self.grid_levels = [lvl for lvl in self.grid_levels if lvl.side == 'sell']



    def summary(self) -> Dict[str, Any]:
        open_buys = sum(1 for l in self.grid_levels if l.status == 'open' and l.side == 'buy')
        open_sells = sum(1 for l in self.grid_levels if l.status == 'open' and l.side == 'sell')
        total_pnl = sum(c['net_pnl'] for c in self.completed_cycles)
        
        levels_list = []
        for l in self.grid_levels:
            if l.status == 'open':
                levels_list.append({
                    'price': l.price,
                    'side': l.side,
                    'qty': l.qty,
                    'size_usd': l.size_usd,
                    'status': l.status,
                    'order_id': l.order_id
                })

        return {
            'symbol': self.symbol,
            'regime': self.current_regime,
            'allocated_usd': float(self.allocated_usd),
            'open_buys': open_buys,
            'open_sells': open_sells,
            'completed_cycles': len(self.completed_cycles),
            'total_net_pnl_usd': total_pnl,
            'current_spacing': getattr(self, 'current_spacing', self.params.grid_spacing),
            'is_weekend_mode': is_weekend_window(),
            'paper_mode': self.paper_mode,
            'levels': levels_list,
        }


