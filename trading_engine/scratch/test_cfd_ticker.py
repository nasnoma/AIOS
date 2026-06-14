import ccxt

def test_ticker():
    # 1. Mainnet standard
    print("--- Testing Mainnet standard ---")
    exchange = ccxt.bybit()
    try:
        ticker = exchange.fetch_ticker("AAPL/USDT:USDT")
        print(f"🟢 Mainnet success! AAPL price: {ticker['last']}")
    except Exception as e:
        print(f"❌ Mainnet failed: {e}")

    # 2. Mainnet with enable_demo_trading
    print("\n--- Testing Mainnet with enable_demo_trading ---")
    exchange_demo = ccxt.bybit()
    exchange_demo.enable_demo_trading(True)
    try:
        ticker = exchange_demo.fetch_ticker("AAPL/USDT:USDT")
        print(f"🟢 Demo success! AAPL price: {ticker['last']}")
    except Exception as e:
        print(f"❌ Demo failed: {e}")

if __name__ == "__main__":
    test_ticker()
