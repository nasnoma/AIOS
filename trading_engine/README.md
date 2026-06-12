# Professional Trading Decision Engine

> 8 specialist AI agents + Judge + Risk Manager — for crypto and stocks

## Architecture

```
Market Data → [8 Parallel Agents] → Judge → Risk Agent → Trade / No Trade
```

| Agent | Indicators | Type |
|-------|-----------|------|
| TrendAgent | EMA20/50/200, HH/HL | Pure quant |
| MomentumAgent | RSI, StochRSI, ROC | Pure quant |
| VolumeAgent | OBV, RelVol, VWAP | Pure quant |
| OrderFlowAgent | OI, Funding, Liquidations | Pure quant |
| VolatilityAgent | ATR, BBWidth, RealVol | Pure quant (veto) |
| StructureAgent | S/R, BOS, FVG | Pure quant |
| SentimentAgent | Fear&Greed, News | LLM-assisted |
| MacroAgent | DXY, VIX, Risk mode | LLM-assisted |
| **Judge** | Weighted ensemble vote | LLM explanation |
| **Risk Agent** | Kelly sizing, ATR stop | Pure quant (veto) |

## Quick Start

### 1. Install dependencies
```bash
cd trading_engine
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure
```bash
cp .env.example .env
# Edit .env with your API keys
```

Minimum required for crypto paper trading (free):
- `OPENAI_API_KEY` — or set `LLM_PROVIDER=anthropic` and add `ANTHROPIC_API_KEY`
- No other keys needed (Binance public endpoints are free)

### 3. Run signal cycle (manual test)
```bash
python -c "from trading_engine.orchestrator import run; r = run('BTC/USDT'); print(r.final_action)"
```

### 4. Run the scheduler (continuous)
```bash
python -m trading_engine.scheduler
```

### 5. Open the dashboard
```bash
# In a separate terminal:
python -m trading_engine.api.server
# Then open: http://localhost:8000
```

### 6. Run backtester
```bash
python -m trading_engine.backtest.engine BTC/USDT 365
```

## Docker (VPS Deployment)

```bash
cd trading_engine
cp .env.example .env
# Fill in .env
docker-compose up -d
```

- Dashboard: `http://your-vps-ip:8000`
- Grafana (optional): `docker-compose --profile monitoring up -d`

## Configuration

Key settings in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `TRADING_MODE` | `paper` | `paper`, `signal_only`, or `live` |
| `DEFAULT_ASSETS` | `BTC/USDT,ETH/USDT` | Crypto watchlist |
| `TIMEFRAME` | `4h` | Chart timeframe |
| `SIGNAL_INTERVAL_MINUTES` | `240` | How often to run full cycle |
| `MIN_AGENT_AGREEMENT` | `6` | Min agents needed to agree (of 8) |
| `MIN_AVG_CONFIDENCE` | `75` | Min average confidence % |
| `MAX_RISK_PER_TRADE` | `0.02` | Max 2% risk per trade |
| `MAX_PORTFOLIO_HEAT` | `0.06` | Max 6% total open risk |
| `KELLY_FRACTION` | `0.25` | Quarter-Kelly position sizing |

## Trading Rules (Hard Rules)
- ✅ Minimum 6/8 agents must agree
- ✅ Average confidence must exceed 75%
- ✅ Risk Agent must approve (ATR stop must be < 8%)
- ✅ Portfolio heat must be < 6%
- ✅ Maximum 5 concurrent positions
- ✅ No trading during extreme volatility (ATR > 8% of price)

## Run Tests
```bash
python -m pytest trading_engine/tests/ -v
```

## File Structure
```
trading_engine/
├── config.py                  # Central config
├── orchestrator.py            # Main pipeline
├── judge.py                   # Weighted ensemble voter
├── risk_agent.py              # Kelly sizing + ATR stops
├── scheduler.py               # APScheduler loop
├── agents/
│   ├── trend_agent.py
│   ├── momentum_agent.py
│   ├── volume_agent.py
│   ├── orderflow_agent.py
│   ├── volatility_agent.py
│   ├── structure_agent.py
│   ├── sentiment_agent.py     # LLM + Fear&Greed
│   └── macro_agent.py         # LLM + DXY/VIX
├── data/
│   └── market_data.py         # OHLCV + indicators (pandas-ta)
├── execution/
│   └── paper_trader.py        # Virtual trading + P&L
├── backtest/
│   └── engine.py              # Vectorized backtester
├── api/
│   ├── server.py              # FastAPI + WebSocket
│   └── templates/dashboard.html
├── alerts/
│   └── telegram_bot.py
├── tests/
│   └── test_agents.py
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```
