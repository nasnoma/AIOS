# Polymarket 5-Min Scalping Bot

A standalone, production-ready trading bot for Polymarket's 5-minute BTC/ETH/SOL Up/Down binary contracts. Captures micro-inefficiencies via spread arbitrage and momentum sniping.

**Completely independent** of any other trading system — standalone config, standalone dependencies.

---

## Strategy Overview

| Strategy | Edge | When |
|---|---|---|
| **Spread Arb** | YES + NO sum < $0.98 → guaranteed profit at resolution | Anytime inside entry window |
| **Momentum** | BTC moves $15+ in 30s; lagging Polymarket odds sniped | Anytime inside entry window |
| **Entry Window** | Only enter at 45s–270s into each 5-min window | Hardcoded guard |

---

## Quick Start

### 1. Install dependencies

```bash
cd polymarket_bot
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
# Edit .env — minimum required for paper trading:
#   TARGET_ASSETS=BTC,ETH
#   TRADING_MODE=paper
#   ACCOUNT_SIZE=1000
# No Polymarket keys needed for paper mode.
```

### 3. Run in paper mode (safe, no real funds)

```bash
python -m polymarket_bot.main
```

### 4. Run tests

```bash
pytest polymarket_bot/tests/ -v
```

---

## Configuration (`.env`)

| Variable | Default | Description |
|---|---|---|
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `ACCOUNT_SIZE` | `1000` | Virtual account size (paper mode) |
| `TARGET_ASSETS` | `BTC,ETH` | Comma-separated: BTC, ETH, SOL |
| `SPREAD_ARB_THRESHOLD` | `0.98` | Buy both legs if YES+NO < this |
| `MOMENTUM_THRESHOLD_USD` | `15.0` | Min BTC move (USD) in 30s |
| `MIN_CONFIDENCE` | `0.70` | Min implied prob on favoured side |
| `MAX_ENTRY_PRICE` | `0.95` | Don't buy above 95¢ |
| `ENTRY_WINDOW_MIN_S` | `45` | Enter after 45s into window |
| `ENTRY_WINDOW_MAX_S` | `270` | Enter before 270s into window |
| `MAX_RISK_PER_TRADE_PCT` | `0.01` | 1% of account per trade |
| `MAX_DAILY_LOSS_USD` | `100` | Daily circuit breaker |
| `POLYMARKET_PRIVATE_KEY` | — | **Required for live mode only** |
| `OPENROUTER_API_KEY` | — | Optional (LLM advisor) |
| `TELEGRAM_BOT_TOKEN` | — | Optional (alerts) |

---

## Live Mode

> ⚠️ Only use live mode after extensive paper trading validation.

1. Fund a Polygon wallet with USDC.
2. Set `POLYMARKET_PRIVATE_KEY` in `.env` (EOA wallet private key).
3. Set `POLYMARKET_SIGNATURE_TYPE=0` (MetaMask/EOA).
4. Change `TRADING_MODE=live`.
5. Keep `MAX_RISK_PER_TRADE_PCT` low (1–2%).

---

## AI Advisor (Optional)

Set `OPENROUTER_API_KEY` and `LLM_MODEL` to enable the post-cycle LLM advisor. It analyses win rate and P&L every N windows (configurable) and logs threshold recommendations. Set `LLM_AUTO_TUNE=true` to auto-apply suggestions.

The LLM runs as a **background async task** — never in the trading hot path.

---

## File Structure

```
polymarket_bot/
├── main.py          # Entry point — runs all async loops
├── config.py        # Pydantic settings from .env
├── market.py        # Gamma API + window timing
├── price_feed.py    # Binance WS BTC/ETH/SOL ticks
├── clob_client.py   # Polymarket CLOB REST + WS
├── strategy.py      # Signal logic (pure functions)
├── risk.py          # Position sizing + circuit breaker
├── execution.py     # Paper fills + live CLOB orders
├── ai_advisor.py    # OpenRouter async LLM advisor
├── state.py         # JSON portfolio state
├── alerts.py        # Telegram notifications
├── tests/           # Unit tests (no network required)
└── .env.example     # Config template
```

---

## Risks

- Execution slippage and gas fees on Polygon reduce actual edge.
- Competition from other bots can erode spread arb opportunities.
- Oracle resolution disputes are rare but possible.
- Start with paper mode. Size positions at 1% of bankroll maximum.
