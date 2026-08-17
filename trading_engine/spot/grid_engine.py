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
        import time
        now = time.time()
        # Throttle rebuilds: don't rebuild more than once per 10 minutes
        # unless grid is completely empty or force=True
        has_open_buys = any(l.status == 'open' and l.side == 'buy' for l in self.grid_levels)
        if not force and has_open_buys and (now - self._last_rebuild_time) < 600:
            return
        self._last_rebuild_time = now

        logger.info(f"Building grid for {self.symbol} at {current_price} in {self.current_regime} regime.")
        
        # Preserve open/pending SELL levels — they represent filled buys awaiting take-profit
        # Only discard old open BUY levels (stale, will be replaced below)
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
        
        # ── Weekend Dip Multiplier (1.45x Spacing & Deeper Wick Targets) ──
        is_wknd = is_weekend_window()
        if is_wknd:
            spacing = spacing * 1.45
            logger.info(f"🌙 Weekend Dip Mode Active [{self.symbol}]: Spacing expanded 1.45x -> {spacing*100:.2f}%")

        self.current_spacing = spacing

        total_levels = self.params.buy_levels + self.params.sell_levels
        raw_order_size = (self.allocated_usd * self.params.capital_pct) / total_levels if total_levels > 0 else 0
        order_size_usd = max(10.0, raw_order_size) if raw_order_size > 0 else 0

        if order_size_usd <= 0:
            return

        
        # Build Buy Levels (Dual-Layer Scalp & Swing Grid Structure):
        # Scalp Layer (Levels 1-3): micro spacing for rapid cycle fills
        # Swing Dip Layer (Levels 4+): wider dip spacing with capital booster for deep dump rebounds
        for i in range(1, self.params.buy_levels + 1):
            if i <= 3:
                scale_factor = 0.65 if is_wknd else 0.45
                price = current_price * (1 - (spacing * scale_factor * i))
                level_boost = 1.00
            else:
                scale_factor = 1.20 if is_wknd else 0.95
                price = current_price * (1 - (spacing * scale_factor * i))
                level_boost = 2.20 if is_wknd else 1.80
                
            lvl_size = order_size_usd * level_boost
            qty = lvl_size / price
            self.grid_levels.append(GridLevel(
                price=price,
                side='buy',
                qty=qty,
                size_usd=lvl_size
            ))

            
        # Build Sell Levels (only for coins already held)
        base_asset = self.symbol.split('/')[0]
        base_qty_held = portfolio.get_position(base_asset) if portfolio and hasattr(portfolio, 'get_position') else 0.0
        
        if base_qty_held > 0 or not portfolio:
            for i in range(1, self.params.sell_levels + 1):
                price = current_price * (1 + spacing * i)
                qty = order_size_usd / price
                self.grid_levels.append(GridLevel(
                    price=price,
                    side='sell',
                    qty=qty,
                    size_usd=order_size_usd,
                    linked_buy_price=current_price
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
                            required_cost = max(6.0, float(min_cost) * 1.15)
                            if price_val > 0 and (qty_val * price_val) < required_cost:
                                qty_val = required_cost / price_val
                                if hasattr(exchange, 'amount_to_precision'):
                                    qty_val = float(exchange.amount_to_precision(self.symbol, qty_val))
                                    prec = exchange.market(self.symbol).get('precision', {}).get('amount')
                                    if isinstance(prec, (float, int)) and float(prec) > 0:
                                        decimals = max(0, -int(math.floor(math.log10(float(prec)))))
                                        qty_val = round(qty_val, decimals)

                        params = {'category': 'spot', 'postOnly': True} if 'bybit' in str(type(exchange)).lower() else {}
                        if level.side == 'buy':
                            order = exchange.create_limit_buy_order(self.symbol, qty_val, price_val, params)
                        else:
                            order = exchange.create_limit_sell_order(self.symbol, qty_val, price_val, params)
                        level.status = 'open'
                        level.order_id = order['id']
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
                    if hasattr(portfolio, 'record_buy'):
                        portfolio.record_buy(self.symbol, level.qty, level.price, level.size_usd, order_id=level.order_id or '')
                    
                    # Fee-aware sell price: net profit retention floor (3.5x fee rate = 0.35% min net profit floor)
                    min_profit_pct = 3.5 * self.fee_rate  # 0.35% net profit floor above entry
                    active_spacing = max(0.009, getattr(self, 'current_spacing', self.params.grid_spacing))
                    sell_price = level.price * (1 + active_spacing + min_profit_pct)
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
                    logger.info(f"BUY filled at {level.price}. Created new SELL level at {sell_price}")
                    
                elif level.side == 'sell':
                    buy_orig_p = level.linked_buy_price or (level.price / (1 + getattr(self, 'current_spacing', self.params.grid_spacing)))
                    if hasattr(portfolio, 'record_sell'):
                        portfolio.record_sell(self.symbol, level.qty, level.price, level.size_usd, level.order_id or '', buy_orig_p)
                        
                    gross_pnl = (level.price - buy_orig_p) * level.qty
                    fee = (level.price * level.qty * self.fee_rate) + (buy_orig_p * level.qty * self.fee_rate)
                    net_pnl = gross_pnl - fee
                    self.completed_cycles.append({
                        'buy_price': buy_orig_p,
                        'sell_price': level.price,
                        'qty': level.qty,
                        'gross_pnl': gross_pnl,
                        'fee': fee,
                        'net_pnl': net_pnl,
                        'timestamp': level.filled_at
                    })
                    logger.info(f"SELL filled at {level.price}. Completed cycle. Net PnL: ${net_pnl:.2f}")
                
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
            if level.status == 'open':
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


