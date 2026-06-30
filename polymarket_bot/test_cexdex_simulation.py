"""
polymarket_bot/test_cexdex_simulation.py

Offline simulation test harness for CEX-DEX Arbitrage strategy.
Verifies spread calculation, route decision, dual-wallet execution, and net P&L math.
"""
import asyncio
from polymarket_bot.state import load_state, save_state, PortfolioState
from polymarket_bot.scanner import CexDexArbitrageScanner
from polymarket_bot.execution import execute_arbitrage

class MockPriceFeed:
    def __init__(self):
        self.bid = 140.0
        self.ask = 140.10
        self.last_update_ts = 0.0

    def get_best_bid_ask(self, symbol: str) -> tuple[float, float, float, float]:
        return self.bid, self.ask, 100.0, 100.0


class MockCexDexScanner(CexDexArbitrageScanner):
    def __init__(self, price_feed):
        super().__init__(price_feed)
        self.mock_jupiter_buy_out = 0.0
        self.mock_jupiter_sell_out = 0.0

    async def get_jupiter_quote(self, input_mint: str, output_mint: str, amount_raw: int) -> int | None:
        # USDT mint -> SOL mint (DEX Buy)
        if input_mint == "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB":
            return int(self.mock_jupiter_buy_out * 1_000_000_000.0)
        # SOL mint -> USDT mint (DEX Sell)
        else:
            return int(self.mock_jupiter_sell_out * 1_000_000.0)


async def run_test():
    print("🧪 Running Bybit-Solana CEX-DEX Arbitrage simulation tests...")

    # 1. Initialize clean split balances
    state = load_state()
    state.cex_cash = 250.0
    state.cex_asset = 2.0
    state.dex_cash = 250.0
    state.dex_asset = 2.0
    state.total_pnl = 0.0
    state.cycle_count = 0
    save_state(state)
    print("✅ Initialized clean split balance: $250 USDT/2.0 SOL on CEX, $250 USDT/2.0 SOL on DEX.")

    feed = MockPriceFeed()
    feed.last_update_ts = asyncio.get_event_loop().time()
    scanner = MockCexDexScanner(feed)

    # ────────────────────────────────────────────────────────
    # Test Route A: DEX Buy, CEX Sell (SOL cheaper on Raydium)
    # Bybit SOLUSDT bid is $140.00
    # Raydium swap cost: $25.00 USDT -> get 0.185 SOL (effective dex_buy_price = 135.13 USDT)
    # Gross spread = (140.00 / 135.13) - 1.0 = +3.60%
    # ────────────────────────────────────────────────────────
    scanner.mock_jupiter_buy_out = 0.185
    scanner.mock_jupiter_sell_out = 0.170 # un-profitable

    print("\n🔎 Scanning for Route A opportunity...")
    opp = await scanner.scan(state)
    assert opp is not None, "Scanner did not detect profitable Route A"
    assert opp["route"] == "DEX-BUY_CEX-SELL", "Incorrect route identified"
    print(f"✅ Route A detected! Net Spread: {opp['net_spread']:.2%}")

    # Execute trade
    await execute_arbitrage(opp, feed, scanner)
    
    # Verify balances
    state = load_state()
    assert state.dex_cash == 225.0, "DEX cash not debited"
    # DEX assets should increase but reflect dynamic slippage and priority fees
    assert 2.15 < state.dex_asset < 2.19, f"DEX asset incorrect: {state.dex_asset}"
    # Bybit Sell: 0.185 SOL * $140.00 * (1 - 0.0010 taker fee) = $25.874 (before slippage)
    assert state.cex_cash > 275.0, "CEX cash did not credit correctly"
    assert state.cex_asset == 1.815, "CEX asset not debited"
    assert state.total_pnl > 0.0, "Realized P&L should be positive"
    print(f"✅ Route A execution successful! Net realized PnL: ${state.total_pnl:+.4f} USDT")

    # ────────────────────────────────────────────────────────
    # Test Route B: CEX Buy, DEX Sell (SOL cheaper on Bybit)
    # Bybit SOLUSDT ask is $140.10
    # Raydium swap proceeds: sell 0.178 SOL -> get $26.50 USDT (effective dex_sell_price = 148.87 USDT)
    # Gross spread = (148.87 / 140.10) - 1.0 = +6.25%
    # ────────────────────────────────────────────────────────
    scanner.mock_jupiter_buy_out = 0.170 # un-profitable
    scanner.mock_jupiter_sell_out = 26.50 # returns USDT directly for input SOL

    # Reset state to clean values
    state.cex_cash = 250.0
    state.cex_asset = 2.0
    state.dex_cash = 250.0
    state.dex_asset = 2.0
    state.total_pnl = 0.0
    save_state(state)

    print("\n🔎 Scanning for Route B opportunity...")
    opp_b = await scanner.scan(state)
    assert opp_b is not None, "Scanner did not detect profitable Route B"
    assert opp_b["route"] == "CEX-BUY_DEX-SELL", "Incorrect route identified"
    print(f"✅ Route B detected! Net Spread: {opp_b['net_spread']:.2%}")

    # Execute trade
    await execute_arbitrage(opp_b, feed, scanner)

    # Verify balances
    state = load_state()
    # Bybit Buy: cost = $25.0 * 1.0010 = $25.025
    assert state.cex_cash == 224.975, "CEX cash not debited correctly"
    assert state.cex_asset > 2.0, "CEX asset not credited"
    # Raydium Sell: proceeds should increase from 250.0 and reflect slippage
    assert state.dex_cash > 275.0, "DEX cash not credited correctly"
    assert state.total_pnl > 0.0, "Realized P&L should be positive"
    print(f"✅ Route B execution successful! Net realized PnL: ${state.total_pnl:+.4f} USDT")

    print("\n🎉 ALL CEX-DEX SIMULATION TESTS PASSED SUCCESSFULLY!")


if __name__ == "__main__":
    asyncio.run(run_test())
