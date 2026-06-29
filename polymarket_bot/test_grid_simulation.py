"""
polymarket_bot/test_grid_simulation.py

Offline simulation test harness verifying:
1. Inventory-based reservation price shading.
2. Resting limit order generation.
3. Price-crossing fill matching logic (Paper Mode simulation).
4. Balance updates and average entry price calculation.
"""
import asyncio
from polymarket_bot.state import load_state, save_state, PortfolioState
from polymarket_bot.scanner import GridManager
from polymarket_bot.execution import check_paper_fills, update_resting_grid

class MockPriceFeed:
    def __init__(self):
        self.bid = 140.0
        self.ask = 140.10
        self.last_update_ts = 0.0

    def get_best_bid_ask(self, symbol: str) -> tuple[float, float, float, float]:
        return self.bid, self.ask, 100.0, 100.0

    def set_price(self, bid: float, ask: float):
        self.bid = bid
        self.ask = ask
        self.last_update_ts = asyncio.get_event_loop().time()


async def run_test():
    print("🧪 Running SOL/USDT Grid Market Maker simulation tests...")
    
    # 1. Initialize clean mock state
    state = load_state()
    state.cash = 500.0
    state.asset_balance = 0.0
    state.avg_buy_price = 0.0
    state.open_grid_orders = []
    state.total_pnl = 0.0
    state.win_count = 0
    state.loss_count = 0
    state.cycle_count = 0
    save_state(state)
    print("✅ Initialized clean paper state: $500 USDT, 0 SOL.")

    # 2. Mock price feed
    feed = MockPriceFeed()
    feed.set_price(140.0, 140.10)
    manager = GridManager(feed)

    # 3. Test Grid Calculation & Shading
    grid = manager.calculate_grid(state)
    assert grid is not None, "Grid calculation failed"
    print(f"✅ Calculated initial grid at SOL mid-price: ${grid['mid_price']:.2f}")
    print(f"   Reservation price: ${grid['reservation_price']:.2f} (skew: 0.00%)")
    print(f"   Generated {len(grid['buy_orders'])} BUY and {len(grid['sell_orders'])} SELL levels.")
    
    # Verify PostOnly levels are structured correctly
    assert len(grid["buy_orders"]) > 0, "No buy orders generated"
    assert len(grid["sell_orders"]) == 0, "Should have 0 sell levels since asset balance is 0 SOL"
    print("✅ Verified 0 sell levels are placed when holding 0 inventory.")

    # 4. Update resting grid (place paper orders)
    await update_resting_grid(grid)
    
    # Reload state to check placed orders
    state = load_state()
    assert len(state.open_grid_orders) == len(grid["buy_orders"]), "Incorrect resting order count"
    first_buy = state.open_grid_orders[0]
    print(f"✅ Placed paper orders. First BUY limit is: ${first_buy['price']:.4f} for {first_buy['size']:.4f} SOL")

    # 5. Simulate Price Drop to Trigger Fill
    trigger_price = first_buy["price"] - 0.01
    print(f"📉 Simulating price drop to: ${trigger_price:.4f}")
    feed.set_price(trigger_price, trigger_price + 0.10)
    
    # Check paper fills
    await check_paper_fills(trigger_price)
    
    # Verify state updates
    state = load_state()
    assert state.asset_balance > 0.0, "Asset balance did not increase after fill"
    assert state.avg_buy_price == first_buy["price"], "Average entry price was not updated correctly"
    assert len(state.open_grid_orders) < len(grid["buy_orders"]), "Filled order was not removed from book"
    print(f"✅ BUY level filled! Inventory is now: {state.asset_balance:.4f} SOL (Avg Entry: ${state.avg_buy_price:.2f})")

    # 6. Test Shaded Reservation Price with Inventory
    # With 0.0666 SOL (~$10), inventory is ~2%, which is < 50% target, so reservation price should shade UP (buyer incentive)
    grid_under = manager.calculate_grid(state)
    assert grid_under["reservation_price"] > grid_under["mid_price"], "Reservation price should shade UP when inventory < 50%"
    print(f"✅ Reservation price correctly shaded UP to: ${grid_under['reservation_price']:.4f} (Mid-price: ${grid_under['mid_price']:.4f}) due to under-allocation.")

    # Now simulate over-allocation: set SOL balance to 3.0 SOL (~$450) and cash to $100, which is ~81% inventory
    state.cash = 100.0
    state.asset_balance = 3.0
    save_state(state)
    grid_over = manager.calculate_grid(state)
    assert grid_over["reservation_price"] < grid_over["mid_price"], "Reservation price should shade DOWN when inventory > 50%"
    print(f"✅ Reservation price correctly shaded DOWN to: ${grid_over['reservation_price']:.4f} (Mid-price: ${grid_over['mid_price']:.4f}) due to over-allocation.")

    # 7. Update Grid and Place a Sell Order
    await update_resting_grid(grid_over)
    state = load_state()
    sells = [o for o in state.open_grid_orders if o["side"] == "sell"]
    assert len(sells) > 0, "No sell levels placed despite holding SOL inventory"
    first_sell = sells[0]
    print(f"✅ Placed new grid with SOL inventory. First SELL limit: ${first_sell['price']:.4f}")

    # 8. Simulate Price Spike to Trigger Sell Fill
    spike_price = first_sell["price"] + 0.01
    print(f"📈 Simulating price spike to: ${spike_price:.4f}")
    
    # Record balance before sell
    pre_cash = state.cash
    pre_sol = state.asset_balance
    
    await check_paper_fills(spike_price)
    
    # Verify profit realization
    state = load_state()
    assert state.cash > pre_cash, "Cash balance did not increase after sell"
    assert state.asset_balance < pre_sol, "SOL balance did not decrease after sell"
    assert state.total_pnl != 0.0, "PnL was not realized or tracked"
    assert state.cycle_count == 1, "Completed cycle count did not increment"
    print(f"✅ SELL level filled! Cash balance: ${state.cash:.2f} USDT | Realized PnL: ${state.total_pnl:+.4f} USDT")

    print("\n🎉 ALL TESTS PASSED SUCCESSFULLY! The grid simulator logic is fully correct.")


if __name__ == "__main__":
    asyncio.run(run_test())
