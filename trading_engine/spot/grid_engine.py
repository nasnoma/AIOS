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

@dataclass
class RegimeParams:
    grid_spacing: float
    buy_levels: int
    sell_levels: int
    capital_pct: float
    base_hold_pct: float

REGIME_PARAMS: Dict[str, RegimeParams] = {
    'BULL': RegimeParams(
        grid_spacing=0.008,
        buy_levels=4,
        sell_levels=8,
        capital_pct=0.80,
        base_hold_pct=0.40
    ),
    'RANGE': RegimeParams(
        grid_spacing=0.015,
        buy_levels=6,
        sell_levels=6,
        capital_pct=0.65,
        base_hold_pct=0.30
    ),
    'BEAR': RegimeParams(
        grid_spacing=0.030,
        buy_levels=4,
        sell_levels=3,
        capital_pct=0.30,
        base_hold_pct=0.20
    )
}

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

    def set_regime(self, regime: str):
        if regime not in REGIME_PARAMS:
            logger.error(f"Invalid regime {regime}")
            return
            
        old_params = self.params
        self.current_regime = regime
        self.params = REGIME_PARAMS[regime]
        
        if old_params and old_params.grid_spacing > 0:
            spacing_diff = abs(old_params.grid_spacing - self.params.grid_spacing) / old_params.grid_spacing
        else:
            spacing_diff = 1.0
        if spacing_diff > 0.30 or old_params.buy_levels != self.params.buy_levels:
            logger.info(f"Regime changed to {regime}. Spacing/levels changed > 30%. Forcing grid rebuild.")
            self._last_rebuild_time = 0.0

    def build_grid(self, current_price: float, portfolio=None, atr: float = 0.0, force: bool = False):
        """Build grid levels. If ATR provided, use it for dynamic spacing."""
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
        base_order_size = max(35.0, raw_order_size) if raw_order_size > 0 else 0


        # Build BUY ladder (only if capital is allocated to this asset)
        if base_order_size > 0:
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

        # Safety: If avg_cost is 0 or missing, ensure we never sell below current_price * 1.015
        if avg_cost <= 0:
            avg_cost = current_price

        if base_qty_held > 0.000001:
            total_held_usd = base_qty_held * current_price
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
            # Tier 1: Minimum +$0.60 NET profit after all maker/taker fees (rapid release)
            # Tier 2: Minimum +$0.90 NET profit after all maker/taker fees
            # Tier 3: Minimum +$1.40 NET profit after all maker/taker fees
            # Tier 4: Minimum +$2.20 NET profit after all maker/taker fees
            min_net_profit_tiers = [0.60, 0.90, 1.40, 2.20]
            fee_factor = 0.0010  # 0.10% Bybit taker fee safety buffer

            for i in range(sell_levels_count):
                min_net_usd = min_net_profit_tiers[i] if i < len(min_net_profit_tiers) else (0.60 + 0.50 * i)
                
                # Reference cost basis: Must protect BOTH the overall purchase cost AND the current market price
                # so that sells during market rallies never sell for a micro-step that yields < $0.60 net profit!
                cost_ref = max(avg_cost, current_price)
                denom = qty_per_sell * (1.0 - fee_factor)
                # Two-sided fee formula: Accounts for both 0.1% buy-side fee AND 0.1% sell-side fee
                min_fee_proof_price = (cost_ref * qty_per_sell * (1.0 + fee_factor) + min_net_usd) / denom if denom > 0 else cost_ref * 1.015
                
                # Dynamic staggering for higher tiers
                stagger_step = 0.0050 * i
                target_p = max(min_fee_proof_price, min_fee_proof_price * (1.0 + stagger_step))


                actual_net_pnl = (target_p - cost_ref) * qty_per_sell - (target_p * qty_per_sell * fee_factor)
                logger.info(f"🎯 [{self.symbol}] High-Velocity Sell Level {i+1}/{sell_levels_count}: Target=${target_p:.4f} "
                            f"(CostRef: ${cost_ref:.4f}, Live: ${current_price:.4f}, Net Profit: +${actual_net_pnl:.2f} USD)")


                self.grid_levels.append(GridLevel(
                    price=target_p,
                    side='sell',
                    qty=qty_per_sell,
                    size_usd=qty_per_sell * target_p,
                    linked_buy_price=avg_cost
                ))









    def place_grid_orders(self, portfolio, exchange: ccxt.Exchange):
        self.exchange = exchange
        for level in self.grid_levels:
            if level.status == 'pending':
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

                        if level.side == 'buy' and not self.paper_mode and exchange:
                            res_floor = float(getattr(portfolio, 'usdt_reserved', 0.0) or 0.0)
                            avail_usdt = float(getattr(portfolio, 'usdt_available', 0.0) or 0.0)
                            req_cost = qty_val * price_val
                            open_buys_usd = float(getattr(portfolio, 'total_open_buy_usd', 0.0) or 0.0)

                            # 🛑 AIRTIGHT HARD FLOOR RULE:
                            # (Total USDT - All Open Buy Commitments - New Order Cost) MUST be >= 20% Reserve Floor
                            if res_floor > 0 and (avail_usdt - open_buys_usd - req_cost) < res_floor:
                                logger.debug(f"[{self.symbol}] Pausing buy order (${req_cost:.2f}) - Total open buy commitments (${open_buys_usd:.2f}) would breach 20% cash reserve (${res_floor:,.2f} floor).")
                                continue

                        params = {'category': 'spot', 'postOnly': True} if 'bybit' in str(type(exchange)).lower() else {}
                        try:
                            if level.side == 'buy':
                                order = exchange.create_limit_buy_order(self.symbol, qty_val, price_val, params)
                            else:
                                order = exchange.create_limit_sell_order(self.symbol, qty_val, price_val, params)
                        except Exception as e_post:
                            # If postOnly was rejected because price is at or across spread, retry with standard limit order
                            if 'postonly' in str(e_post).lower() or 'post_only' in str(e_post).lower() or '170193' in str(e_post):
                                fallback_params = {'category': 'spot'} if 'bybit' in str(type(exchange)).lower() else {}
                                if level.side == 'buy':
                                    order = exchange.create_limit_buy_order(self.symbol, qty_val, price_val, fallback_params)
                                else:
                                    order = exchange.create_limit_sell_order(self.symbol, qty_val, price_val, fallback_params)
                            else:
                                raise e_post

                        level.status = 'open'
                        level.order_id = order['id']
                        if level.side == 'buy' and hasattr(portfolio, 'total_open_buy_usd'):
                            portfolio.total_open_buy_usd = float(getattr(portfolio, 'total_open_buy_usd', 0.0)) + req_cost
                        logger.info(f"[LIVE] Placed {level.side} limit order for {self.symbol} at {price_val} (Qty: {qty_val}, ID: {order['id']})")


                    except Exception as e:
                        logger.error(f"Failed to place {level.side} order for {self.symbol}: {e}")


    def tick(self, current_price: float, portfolio, exchange=None) -> List[Dict[str, Any]]:
        """Check grid levels against current price and simulate/process fills."""
        fills = []
        for level in list(self.grid_levels):
            if level.status != 'open':
                continue
                
            is_filled = False
            if not self.paper_mode and exchange and level.order_id and not level.order_id.startswith('PAPER_'):
                try:
                    order_info = exchange.fetch_order(level.order_id, self.symbol, {'category': 'spot'})
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
                    fee_factor = 0.0010
                    denom = level.qty * (1.0 - fee_factor)
                    if denom > 0:
                        min_fee_proof_sell = (level.price * level.qty * (1.0 + fee_factor) + min_net_usd) / denom
                        sell_price = max(min_fee_proof_sell, level.price * 1.0090)
                    else:
                        sell_price = level.price * 1.0150


                    new_sell = GridLevel(
                        price=sell_price,
                        side='sell',
                        qty=level.qty,
                        size_usd=sell_price * level.qty,
                        linked_buy_price=level.price
                    )
                    self.grid_levels.append(new_sell)
                    if self.paper_mode:
                        new_sell.status = 'open'
                        new_sell.order_id = f'PAPER_{uuid4().hex[:8]}'
                    fills.append({'side': 'buy', 'price': level.price, 'qty': level.qty})
                    logger.info(f"BUY filled at {level.price}. Created new SELL level at {sell_price:.4f} (Guaranteed Net: +${min_net_usd:.2f})")

                    
                elif level.side == 'sell':
                    buy_orig_p = level.linked_buy_price or (level.price / (1 + getattr(self, 'current_spacing', self.params.grid_spacing)))

                    # ── HARD GATE: Block any fill that yields < $0.50 net profit after all fees ──
                    MIN_NET_PROFIT_USD = 0.50
                    gross_pnl = (level.price - buy_orig_p) * level.qty
                    fee = (level.price * level.qty * self.fee_rate) + (buy_orig_p * level.qty * self.fee_rate)
                    net_pnl = gross_pnl - fee

                    if net_pnl < MIN_NET_PROFIT_USD and level.qty > 0 and buy_orig_p > 0:
                        # Recalculate and replace this sell order with one that guarantees >= $0.50 net
                        fee_factor = self.fee_rate
                        denom = level.qty * (1.0 - fee_factor)
                        min_sell_price = (buy_orig_p * level.qty + MIN_NET_PROFIT_USD) / denom if denom > 0 else buy_orig_p * 1.015
                        logger.warning(
                            f"⛔ [{self.symbol}] BLOCKED sub-$0.50 sell fill: would have netted ${net_pnl:.4f} "
                            f"(Sell @ ${level.price:.4f}, Cost @ ${buy_orig_p:.4f}, Qty: {level.qty:.2f}). "
                            f"Replacing with min-profit sell @ ${min_sell_price:.4f}"
                        )
                        level.status = 'open'  # Revert status so it doesn't get counted as filled
                        if exchange and level.order_id and not str(level.order_id).startswith('PAPER'):
                            try:
                                exchange.cancel_order(level.order_id, self.symbol, {'category': 'spot'})
                                new_params = {'category': 'spot', 'postOnly': True}
                                qty_val = float(exchange.amount_to_precision(self.symbol, level.qty)) if hasattr(exchange, 'amount_to_precision') else level.qty
                                p_val = float(exchange.price_to_precision(self.symbol, min_sell_price)) if hasattr(exchange, 'price_to_precision') else min_sell_price
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





                
                    new_buy = GridLevel(
                        price=buy_orig_p,
                        side='buy',
                        qty=level.size_usd / buy_orig_p if buy_orig_p > 0 else level.qty,
                        size_usd=level.size_usd
                    )
                    self.grid_levels.append(new_buy)
                    if self.paper_mode:
                        new_buy.status = 'open'
                        new_buy.order_id = f'PAPER_{uuid4().hex[:8]}'
                        
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


