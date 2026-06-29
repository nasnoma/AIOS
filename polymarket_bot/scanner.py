"""
polymarket_bot/scanner.py

Grid Manager engine for SOL/USDT Spot Grid Market Making.
Calculates reservation prices, inventory skew, and generates grid order levels.
"""
from __future__ import annotations
import asyncio
from loguru import logger

from polymarket_bot.config import settings
from polymarket_bot.price_feed import BybitPriceFeed
from polymarket_bot.state import PortfolioState


class GridManager:
    def __init__(self, price_feed: BybitPriceFeed):
        self.price_feed = price_feed

    def calculate_grid(self, state: PortfolioState) -> dict | None:
        """
        Calculates resting limit buy and sell levels based on mid-price and inventory shading.
        Returns a dict containing grid buy/sell levels, or None if data is stale.
        """
        # Fetch SOLUSDT ticker
        bid, ask, _, _ = self.price_feed.get_best_bid_ask("SOLUSDT")
        if not bid or not ask:
            return None

        # Check freshness of prices
        now = asyncio.get_event_loop().time()
        price_age = now - self.price_feed.last_update_ts
        if price_age > settings.max_price_age_s:
            logger.warning(f"⚠️ Stale market prices (Age: {price_age:.2f}s > limit {settings.max_price_age_s}s). Skipping grid calculation.")
            return None

        mid_price = (bid + ask) / 2.0

        # Calculate inventory skew and reservation price (Avellaneda-Stoikov style)
        sol_value = state.asset_balance * mid_price
        total_equity = state.cash + sol_value
        
        current_inv_ratio = sol_value / total_equity if total_equity > 0 else 0.5
        inv_imbalance = settings.inventory_target_pct - current_inv_ratio
        
        # Shade reservation price: positive imbalance (need asset) -> shade up (buy higher)
        # negative imbalance (too much asset) -> shade down (sell lower)
        skew = inv_imbalance * settings.inventory_shading_factor
        reservation_price = mid_price * (1.0 + skew)

        # Generate Grid Levels
        levels = settings.grid_levels
        # step size as a fraction (e.g. 1.5% total span / 5 levels = 0.3% per level)
        step_fraction = settings.grid_span_pct / levels

        buy_orders = []
        sell_orders = []

        remaining_cash = state.cash
        remaining_asset = state.asset_balance

        # 1. Place Buy Orders (Below Reservation Price)
        for i in range(levels):
            # buy price decreases as we go deeper
            buy_price = reservation_price * (1.0 - (i + 1) * step_fraction)
            buy_size = settings.order_size_usdt / buy_price
            
            # Check if we have enough USDT to place this level
            if remaining_cash >= settings.order_size_usdt:
                buy_orders.append({
                    "price": round(buy_price, 4),
                    "size": round(buy_size, 4),
                    "side": "buy"
                })
                remaining_cash -= settings.order_size_usdt
            else:
                break

        # 2. Place Sell Orders (Above Reservation Price)
        for i in range(levels):
            # sell price increases as we go deeper
            sell_price = reservation_price * (1.0 + (i + 1) * step_fraction)
            sell_size = settings.order_size_usdt / sell_price
            
            # Check if we have enough SOL to sell this level
            if remaining_asset >= sell_size:
                sell_orders.append({
                    "price": round(sell_price, 4),
                    "size": round(sell_size, 4),
                    "side": "sell"
                })
                remaining_asset -= sell_size
            else:
                break

        return {
            "mid_price": mid_price,
            "reservation_price": reservation_price,
            "current_inv_ratio": current_inv_ratio,
            "total_equity": total_equity,
            "buy_orders": buy_orders,
            "sell_orders": sell_orders
        }
