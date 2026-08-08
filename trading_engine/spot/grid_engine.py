from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
import ccxt
from datetime import datetime, timezone
from loguru import logger
from uuid import uuid4

@dataclass
class RegimeParams:
    grid_spacing: float
    buy_levels: int
    sell_levels: int
    capital_pct: float
    base_hold_pct: float

REGIME_PARAMS: Dict[str, RegimeParams] = {
    'BULL': RegimeParams(
        grid_spacing=0.006,
        buy_levels=4,
        sell_levels=8,
        capital_pct=0.80,
        base_hold_pct=0.40
    ),
    'RANGE': RegimeParams(
        grid_spacing=0.010,
        buy_levels=8,
        sell_levels=8,
        capital_pct=0.65,
        base_hold_pct=0.30
    ),
    'BEAR': RegimeParams(
        grid_spacing=0.025,
        buy_levels=5,
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

    def set_regime(self, regime: str):
        if regime not in REGIME_PARAMS:
            logger.error(f"Invalid regime {regime}")
            return
            
        old_params = self.params
        self.current_regime = regime
        self.params = REGIME_PARAMS[regime]
        
        spacing_diff = abs(old_params.grid_spacing - self.params.grid_spacing) / old_params.grid_spacing
        if spacing_diff > 0.30:
            logger.info(f"Regime changed to {regime}. Spacing changed > 30%. Need to rebuild grid.")
            # Note: actual grid rebuild would require current_price, normally called separately

    def build_grid(self, current_price: float, portfolio=None, atr: float = 0.0):
        """Build grid levels. If ATR provided, use it for dynamic spacing."""
        logger.info(f"Building grid for {self.symbol} at {current_price} in {self.current_regime} regime.")
        
        self.grid_levels = [level for level in self.grid_levels if level.status in ['filled']]
        
        # ── Dynamic spacing: use ATR if available, else config spacing ──
        # Research: ATR-based grid spacing adapts to volatility regime,
        # prevents tight grids getting stopped in high-vol and wide grids
        # missing fills in low-vol. Floor at 0.3% to stay above fees.
        if atr > 0 and current_price > 0:
            atr_pct = atr / current_price
            dynamic_spacing = max(0.003, min(atr_pct * 0.8, 0.05))  # 0.3% to 5%
            # Blend with regime spacing: 50% ATR, 50% regime default
            spacing = (dynamic_spacing + self.params.grid_spacing) / 2
        else:
            spacing = self.params.grid_spacing
        
        total_levels = self.params.buy_levels + self.params.sell_levels
        order_size_usd = (self.allocated_usd * self.params.capital_pct) / total_levels if total_levels > 0 else 0
        
        # Build Buy Levels (geometric spacing below current price)
        for i in range(1, self.params.buy_levels + 1):
            price = current_price * (1 - spacing * i)
            qty = order_size_usd / price
            self.grid_levels.append(GridLevel(
                price=price,
                side='buy',
                qty=qty,
                size_usd=order_size_usd
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
                    size_usd=order_size_usd
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
                        if level.side == 'buy':
                            order = exchange.create_limit_buy_order(self.symbol, level.qty, level.price)
                        else:
                            order = exchange.create_limit_sell_order(self.symbol, level.qty, level.price)
                        level.status = 'open'
                        level.order_id = order['id']
                        logger.info(f"[LIVE] Placed {level.side} limit order for {self.symbol} at {level.price}")
                    except Exception as e:
                        logger.error(f"Failed to place {level.side} order: {e}")

    def tick(self, current_price: float, portfolio) -> List[Dict[str, Any]]:
        fills = []
        for level in self.grid_levels:
            if level.status != 'open':
                continue
                
            is_filled = False
            if level.side == 'buy' and current_price <= level.price:
                is_filled = True
            elif level.side == 'sell' and current_price >= level.price:
                is_filled = True
                
            if is_filled:
                level.status = 'filled'
                level.filled_at = datetime.now(timezone.utc).isoformat()
                
                if level.side == 'buy':
                    if hasattr(portfolio, 'record_buy'):
                        portfolio.record_buy(self.symbol, level.qty, level.price, level.size_usd * self.fee_rate)
                    
                    # Fee-aware sell price: profit must exceed 2× round-trip fees
                    # Round-trip fee = buy fee + sell fee = 2 × fee_rate × notional
                    # Min profit = spacing (price gain) must be > 2 × fee_rate
                    min_profit_pct = 2 * self.fee_rate  # break-even on fees
                    safety_margin_pct = 0.002  # 0.2% extra profit margin
                    sell_price = level.price * (1 + self.params.grid_spacing + min_profit_pct + safety_margin_pct)
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
                    if hasattr(portfolio, 'record_sell'):
                        portfolio.record_sell(self.symbol, level.qty, level.price, level.size_usd * self.fee_rate)
                        
                    if level.linked_buy_price:
                        gross_pnl = (level.price - level.linked_buy_price) * level.qty
                        fee = (level.price * level.qty * self.fee_rate) + (level.linked_buy_price * level.qty * self.fee_rate)
                        net_pnl = gross_pnl - fee
                        self.completed_cycles.append({
                            'buy_price': level.linked_buy_price,
                            'sell_price': level.price,
                            'qty': level.qty,
                            'gross_pnl': gross_pnl,
                            'fee': fee,
                            'net_pnl': net_pnl,
                            'timestamp': level.filled_at
                        })
                        logger.info(f"SELL filled at {level.price}. Completed cycle. Net PnL: ${net_pnl:.2f}")
                    
                        new_buy = GridLevel(
                            price=level.linked_buy_price,
                            side='buy',
                            qty=level.size_usd / level.linked_buy_price,
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
        for level in self.grid_levels:
            if level.status == 'open':
                if self.paper_mode:
                    level.status = 'cancelled'
                    logger.info(f"[PAPER] Cancelled order {level.order_id}")
                else:
                    if exch and level.order_id:
                        try:
                            exch.cancel_order(level.order_id, self.symbol)
                            level.status = 'cancelled'
                            logger.info(f"[LIVE] Cancelled order {level.order_id}")
                        except Exception as e:
                            logger.error(f"Failed to cancel {level.order_id}: {e}")

    def summary(self) -> Dict[str, Any]:
        open_buys = sum(1 for l in self.grid_levels if l.status == 'open' and l.side == 'buy')
        open_sells = sum(1 for l in self.grid_levels if l.status == 'open' and l.side == 'sell')
        total_pnl = sum(c['net_pnl'] for c in self.completed_cycles)
        
        return {
            'symbol': self.symbol,
            'regime': self.current_regime,
            'open_buys': open_buys,
            'open_sells': open_sells,
            'completed_cycles': len(self.completed_cycles),
            'total_net_pnl_usd': total_pnl,
            'paper_mode': self.paper_mode
        }
