import json
import datetime
from dataclasses import dataclass, asdict
from typing import List, Dict, Optional
from loguru import logger

@dataclass
class AssetHolding:
    symbol: str
    units_held: float
    avg_cost_basis: float
    base_hold_units: float
    last_price: float
    
    @property
    def value_usd(self) -> float:
        return self.units_held * self.last_price
        
    @property
    def base_value_usd(self) -> float:
        return self.base_hold_units * self.last_price
        
    @property
    def tradeable_units(self) -> float:
        return self.units_held - self.base_hold_units
        
    @property
    def unrealised_pnl(self) -> float:
        return (self.last_price - self.avg_cost_basis) * self.units_held
        
    @property
    def unrealised_pnl_pct(self) -> float:
        if self.avg_cost_basis > 0 and self.units_held > 0:
            return self.unrealised_pnl / (self.avg_cost_basis * self.units_held)
        return 0.0

@dataclass
class GridOrder:
    order_id: str
    symbol: str
    side: str
    price: float
    qty: float
    size_usd: float
    status: str
    created_at: str
    filled_at: Optional[str] = None
    profit_usd: Optional[float] = None

class SpotPortfolio:
    def __init__(self, state_path: str = None):
        if state_path is None:
            from pathlib import Path
            state_path = str(Path(__file__).parent.parent / "spot_state.json")
        self.state_path = state_path
        self.holdings: Dict[str, AssetHolding] = {}
        self.usdt_available: float = 0.0
        self.usdt_reserved: float = 0.0
        self.grid_orders: List[GridOrder] = []
        self.dca_orders: List[GridOrder] = []
        self.total_realised_pnl: float = 0.0
        self.daily_realised_pnl: float = 0.0
        self.cycles_today: int = 0
        self.consecutive_wins: int = 0
        self.consecutive_losses: int = 0
        self.completed_cycles: List[dict] = []
        self.last_daily_reset: str = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        self.load()
        
    def _build_state_dict(self) -> dict:
        return {
            'usdt_available': self.usdt_available,
            'usdt_reserved': self.usdt_reserved,
            'total_realised_pnl': self.total_realised_pnl,
            'daily_realised_pnl': self.daily_realised_pnl,
            'cycles_today': self.cycles_today,
            'consecutive_wins': getattr(self, 'consecutive_wins', 0),
            'consecutive_losses': getattr(self, 'consecutive_losses', 0),
            'completed_cycles': getattr(self, 'completed_cycles', [])[-50:],
            'last_daily_reset': self.last_daily_reset,
            'holdings': {k: asdict(v) for k, v in self.holdings.items()},
            'grid_orders': [asdict(o) for o in self.grid_orders],
            'dca_orders': [asdict(o) for o in self.dca_orders]
        }

    def _apply_state_dict(self, data: dict):
        self.usdt_available = data.get('usdt_available', 0.0)
        self.usdt_reserved = data.get('usdt_reserved', 0.0)
        self.total_realised_pnl = data.get('total_realised_pnl', 0.0)
        self.daily_realised_pnl = data.get('daily_realised_pnl', 0.0)
        self.cycles_today = data.get('cycles_today', 0)
        self.consecutive_wins = data.get('consecutive_wins', 0)
        self.consecutive_losses = data.get('consecutive_losses', 0)
        self.completed_cycles = data.get('completed_cycles', [])
        self.last_daily_reset = data.get('last_daily_reset', datetime.datetime.now(datetime.timezone.utc).date().isoformat())
        self.holdings = {k: AssetHolding(**v) for k, v in data.get('holdings', {}).items()}
        self.grid_orders = [GridOrder(**o) for o in data.get('grid_orders', [])]
        self.dca_orders = [GridOrder(**o) for o in data.get('dca_orders', [])]

    def load(self):
        # 1. Try PostgreSQL first (survives Railway restarts)
        try:
            from trading_engine.storage.db import get_portfolio_state
            data = get_portfolio_state('spot_portfolio')
            if data:
                self._apply_state_dict(data)
                logger.info(f"Loaded spot portfolio from DB: {len(self.holdings)} holdings, {len(self.grid_orders)} grid orders")
                return
        except Exception as e:
            logger.debug(f"DB load failed, falling back to JSON: {e}")
        # 2. Fallback to local JSON file
        try:
            with open(self.state_path, 'r') as f:
                data = json.load(f)
            self._apply_state_dict(data)
            logger.info(f"Loaded spot portfolio from JSON: {len(self.holdings)} holdings, {len(self.grid_orders)} grid orders")
        except FileNotFoundError:
            logger.info(f"State file {self.state_path} not found, initializing fresh portfolio")
        except Exception as e:
            logger.error(f"Error loading portfolio state: {e}")
            
    def save(self):
        data = self._build_state_dict()
        # 1. Save to PostgreSQL (primary — survives Railway restarts)
        try:
            from trading_engine.storage.db import save_portfolio_state
            save_portfolio_state('spot_portfolio', data)
        except Exception as e:
            logger.debug(f"DB save failed, falling back to JSON: {e}")
        # 2. Also save to local JSON file as backup
        try:
            with open(self.state_path, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.debug(f"JSON save failed: {e}")
            
    def update_price(self, symbol: str, price: float):
        if symbol in self.holdings:
            self.holdings[symbol].last_price = price
            
    def record_buy(self, symbol: str, qty: float, price: float, size_usd: float, order_id: str = '', is_dca: bool = False):
        self.usdt_available -= size_usd
        if symbol not in self.holdings:
            self.holdings[symbol] = AssetHolding(
                symbol=symbol,
                units_held=0.0,
                avg_cost_basis=0.0,
                base_hold_units=0.0,
                last_price=price
            )
            
        h = self.holdings[symbol]
        total_cost = (h.units_held * h.avg_cost_basis) + size_usd
        h.units_held += qty
        h.avg_cost_basis = total_cost / h.units_held if h.units_held > 0 else 0.0
        h.last_price = price
        
        order = GridOrder(
            order_id=order_id,
            symbol=symbol,
            side='buy',
            price=price,
            qty=qty,
            size_usd=size_usd,
            status='filled',
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            filled_at=datetime.datetime.now(datetime.timezone.utc).isoformat()
        )
        if is_dca:
            self.dca_orders.append(order)
        else:
            self.grid_orders.append(order)
            
        self.save()
        
    def record_sell(self, symbol: str, qty: float, price: float, size_usd: float, order_id: str, buy_cost_basis: float):
        if symbol not in self.holdings or self.holdings[symbol].units_held < qty:
            logger.error(f"Cannot sell {qty} {symbol}: insufficient holdings")
            return
            
        h = self.holdings[symbol]
        h.units_held -= qty
        h.last_price = price
        if h.units_held <= 0.000001:
            h.units_held = 0.0
            h.avg_cost_basis = 0.0
            
        profit = size_usd - (qty * buy_cost_basis)
        self.usdt_available += size_usd
        self.total_realised_pnl += profit
        
        self.reset_daily_if_needed()
        self.daily_realised_pnl += profit
        self.cycles_today += 1

        if not hasattr(self, 'completed_cycles'):
            self.completed_cycles = []
            
        self.completed_cycles.append({
            'symbol': symbol,
            'buy_price': buy_cost_basis,
            'sell_price': price,
            'qty': qty,
            'net_pnl': profit,
            'timestamp': datetime.datetime.now(datetime.timezone.utc).isoformat()
        })

        # Dan Cheung Streak Tracking:
        if profit > 0:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
        
        order = GridOrder(
            order_id=order_id,
            symbol=symbol,
            side='sell',
            price=price,
            qty=qty,
            size_usd=size_usd,
            status='filled',
            created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            filled_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            profit_usd=profit
        )
        self.grid_orders.append(order)
        self.save()
        
    def reset_daily_if_needed(self):
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        if self.last_daily_reset != today:
            self.daily_realised_pnl = 0.0
            self.cycles_today = 0
            self.consecutive_wins = 0
            self.consecutive_losses = 0
            self.last_daily_reset = today
            
    def get_streak_risk_factor(self) -> float:
        """
        Dan Cheung 3-Step Risk System (gGjefoUjnJI):
        De-escalate risk/position size by 50% during losing streaks (consecutive_losses >= 2)
        Scale up position size by 1.15x during winning streaks (consecutive_wins >= 2).
        """
        if self.consecutive_losses >= 2:
            return 0.50  # Risk De-escalation Protection
        elif self.consecutive_wins >= 2:
            return 1.15  # Win-Streak Momentum Scaling
        return 1.0

    def open_grid_orders(self, symbol: Optional[str] = None) -> List[GridOrder]:
        orders = [o for o in self.grid_orders if o.status == 'open']
        if symbol:
            orders = [o for o in orders if o.symbol == symbol]
        return orders
        
    def summary(self) -> dict:
        self.reset_daily_if_needed()
        formatted_holdings = {}
        for k, v in self.holdings.items():
            if isinstance(v, dict):
                u = float(v.get('units_held') or v.get('units') or 0.0)
                cost = float(v.get('avg_cost_basis') or 0.0)
                price = float(v.get('last_price') or 0.0)
                val = float(v.get('value_usd') or (u * price))
                if cost <= 0.0 and u > 0 and val > 0:
                    cost = val / u
                pnl = (price - cost) * u
                pnl_pct = (pnl / (cost * u)) if (cost * u) > 0 else 0.0
            else:
                u = float(getattr(v, 'units_held', getattr(v, 'units', 0.0)))
                cost = float(getattr(v, 'avg_cost_basis', 0.0))
                price = float(getattr(v, 'last_price', 0.0))
                val = float(getattr(v, 'value_usd', u * price))
                if cost <= 0.0 and u > 0 and val > 0:
                    cost = val / u
                pnl = (price - cost) * u
                pnl_pct = (pnl / (cost * u)) if (cost * u) > 0 else 0.0
            formatted_holdings[k] = {
                'symbol': k,
                'units': u,
                'units_held': u,
                'avg_cost_basis': cost,
                'last_price': price,
                'value_usd': val,
                'unrealised_pnl': pnl,
                'unrealised_pnl_pct': pnl_pct
            }
        return {
            'usdt_available': self.usdt_available,
            'usdt_reserved': self.usdt_reserved,
            'total_realised_pnl': self.total_realised_pnl,
            'daily_realised_pnl': self.daily_realised_pnl,
            'cycles_today': self.cycles_today,
            'completed_cycles': getattr(self, 'completed_cycles', [])[-20:],
            'holdings': formatted_holdings
        }
