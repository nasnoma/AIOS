"""
polymarket_bot/backtester.py

Historical backtester for the Polymarket 5-min momentum strategy.

Data: data/historical_1m_{ASSET}_USDT.csv  (preferred, fetch with Binance API)
      data/historical_5m_{ASSET}_USDT.csv  (fallback)
      Columns: timestamp, open, high, low, close, volume

Simulation model (no lookahead bias):
  - Group 1-minute bars into 5-minute windows (aligned to 5-min grid)
  - Strike         = window open (first 1m bar's open)
  - Entry price    = bar[ENTRY_MINUTE].close  (~60s into window)
  - Momentum       = entry_price - strike  (same as live bot: spot - strike)
  - YES mid price  = clip(0.50 + momentum / PRICE_SENSITIVITY, 0.40, 0.60)
  - Resolution     = YES if window_close >= strike, else NO
  - Entry spread   = ±HALF_SPREAD on each side

Uses evaluate_signals() from strategy.py directly — same code as live bot.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from polymarket_bot.clob_client import OrderBook, OrderBookLevel
from polymarket_bot.config import Settings
from polymarket_bot.strategy import SignalType, evaluate_signals

# ── Constants ─────────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent.parent / "data"
HALF_SPREAD = 0.005          # bid/ask spread each side
PRICE_SENSITIVITY = 200.0    # $ momentum → fraction of YES price shift
ENTRY_MINUTE = 1             # which 1m bar to use for signal (0=first, 1=second)
FEE_RATE = 0.02              # Polymarket taker fee (2%)
WINDOW_SECONDS = 300         # 5-minute windows


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class Bar:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Window:
    """Five consecutive 1-minute bars forming one 5-minute trading window."""
    bars: List[Bar]

    @property
    def strike(self) -> float:
        return self.bars[0].open

    @property
    def entry_bar(self) -> Bar:
        return self.bars[min(ENTRY_MINUTE, len(self.bars) - 1)]

    @property
    def entry_elapsed_s(self) -> float:
        return (ENTRY_MINUTE + 1) * 60.0  # e.g. 120s for ENTRY_MINUTE=1

    @property
    def entry_remaining_s(self) -> float:
        return WINDOW_SECONDS - self.entry_elapsed_s

    @property
    def final_close(self) -> float:
        return self.bars[-1].close

    @property
    def yes_wins(self) -> bool:
        return self.final_close >= self.strike


@dataclass
class TradeRecord:
    asset: str
    signal_type: str
    entry_price: float
    size_usd: float
    pnl: float
    win: bool


@dataclass
class BacktestResult:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    score: float = 0.0
    trades: List[TradeRecord] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades if self.total_trades > 0 else 0.0


# ── Loaders ───────────────────────────────────────────────────────────────────

def _load_bars_csv(path: Path) -> List[Bar]:
    bars: List[Bar] = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_str = row["timestamp"].replace("+00:00", "").replace("Z", "")
            try:
                ts = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            bars.append(Bar(
                ts=ts,
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            ))
    return bars


def load_bars(asset: str) -> List[Bar]:
    """Load bars, preferring 1m data, falling back to 5m."""
    for suffix in ["1m", "5m"]:
        path = DATA_DIR / f"historical_{suffix}_{asset}_USDT.csv"
        if path.exists():
            return _load_bars_csv(path)
    raise FileNotFoundError(f"No data file found for {asset} in {DATA_DIR}")


def group_into_windows(bars: List[Bar]) -> List[Window]:
    """Group bars into 5-minute windows aligned to UNIX 5-min grid."""
    buckets: Dict[int, List[Bar]] = {}
    for bar in bars:
        ts_unix = int(bar.ts.timestamp())
        window_start = ts_unix - (ts_unix % WINDOW_SECONDS)
        buckets.setdefault(window_start, []).append(bar)

    windows = []
    for wstart in sorted(buckets):
        bucket = sorted(buckets[wstart], key=lambda b: b.ts)
        if len(bucket) >= 3:  # need entry bar + at least one resolution bar
            windows.append(Window(bars=bucket))
    return windows


# ── Market simulation ─────────────────────────────────────────────────────────

def _synthetic_book(mid: float) -> OrderBook:
    bid = round(mid - HALF_SPREAD, 4)
    ask = round(mid + HALF_SPREAD, 4)
    return OrderBook(
        token_id="synthetic",
        bids=[OrderBookLevel(price=bid, size=1000.0)],
        asks=[OrderBookLevel(price=ask, size=1000.0)],
    )


def _yes_mid(momentum: float) -> float:
    """Map intra-window momentum (USD vs strike) to implied YES probability."""
    raw = 0.50 + momentum / PRICE_SENSITIVITY
    return max(0.40, min(0.60, raw))


# ── Core simulation ───────────────────────────────────────────────────────────

def _simulate_window(
    window: Window,
    asset: str,
    cfg: Settings,
    cash: float,
    account_size: float,
    daily_pnl: float,
) -> Optional[TradeRecord]:
    """
    Simulate one 5-minute trading window using intra-window price vs strike.
    Mirrors the live bot: momentum = entry_bar.close - strike.
    No lookahead: only bars up to ENTRY_MINUTE inform the decision.
    """
    momentum = window.entry_bar.close - window.strike

    yes_mid = _yes_mid(momentum)
    no_mid = 1.0 - yes_mid
    book_yes = _synthetic_book(yes_mid)
    book_no = _synthetic_book(no_mid)

    signal = evaluate_signals(
        asset=asset,
        book_yes=book_yes,
        book_no=book_no,
        momentum_usd=momentum,
        elapsed_s=window.entry_elapsed_s,
        remaining_s=window.entry_remaining_s,
        cfg=cfg,
    )

    if signal.signal_type == SignalType.NO_SIGNAL:
        return None

    # Circuit breaker & Session drawdown guardrail check (mirrors risk.py)
    if daily_pnl <= -abs(cfg.max_daily_loss_usd) or daily_pnl <= -20.0:
        return None

    # Sizing (mirrors risk.py)
    base_size = account_size * cfg.max_risk_per_trade_pct
    base_size = min(base_size, 5.00) # Cap base size at $5.00
    size_factor = 1.0
    if signal.confidence < 0.45:
        size_factor *= 0.5
    if daily_pnl < 0:
        size_factor *= 0.5
    size = max(1.0, round(min(base_size * size_factor, cash), 2))

    if signal.signal_type == SignalType.MOMENTUM_LONG:
        entry_price = yes_mid + HALF_SPREAD
        win = window.yes_wins
    elif signal.signal_type == SignalType.MOMENTUM_SHORT:
        entry_price = no_mid + HALF_SPREAD
        win = not window.yes_wins
    else:
        return None

    shares = size / entry_price
    payout = 1.0 if win else 0.0
    pnl = round(shares * (payout - entry_price) * (1.0 - FEE_RATE if win else 1.0), 4)

    return TradeRecord(
        asset=asset,
        signal_type=signal.signal_type.value,
        entry_price=round(entry_price, 4),
        size_usd=size,
        pnl=pnl,
        win=win,
    )


# ── Public API ────────────────────────────────────────────────────────────────

def run_backtest(
    assets: List[str],
    cfg: Settings,
    account_size: float = 1000.0,
    max_bars: Optional[int] = None,
) -> BacktestResult:
    """
    Run a full backtest over historical price data.

    Args:
        assets:       e.g. ["BTC", "ETH"]
        cfg:          Strategy settings to evaluate
        account_size: Starting balance in USD
        max_bars:     Limit 1m bars loaded per asset (None = all)

    Returns:
        BacktestResult with P&L, win rate, drawdown, and composite score
    """
    result = BacktestResult()
    cash = account_size
    daily_pnl = 0.0
    peak_cash = account_size

    for asset in assets:
        try:
            bars = load_bars(asset)
            if max_bars:
                bars = bars[:max_bars]
            windows = group_into_windows(bars)
        except FileNotFoundError:
            continue

        for window in windows:
            trade = _simulate_window(window, asset, cfg, cash, account_size, daily_pnl)
            if trade is None:
                continue

            result.trades.append(trade)
            result.total_trades += 1
            cash += trade.pnl
            daily_pnl += trade.pnl

            if trade.win:
                result.wins += 1
            else:
                result.losses += 1

            result.net_pnl = round(cash - account_size, 4)
            peak_cash = max(peak_cash, cash)
            drawdown_pct = (peak_cash - cash) / peak_cash if peak_cash > 0 else 0.0
            result.max_drawdown_pct = max(result.max_drawdown_pct, drawdown_pct)

    wr = result.win_rate
    result.score = round(
        result.net_pnl * max(0.0, wr - 0.50) / (1.0 + result.max_drawdown_pct),
        4,
    )
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from polymarket_bot.config import settings

    print("Running backtest with 1m bars + current .env settings...")
    r = run_backtest(assets=["BTC", "ETH"], cfg=settings)
    print(f"  Windows   : {r.total_trades}")
    print(f"  Win rate  : {r.win_rate:.1%}")
    print(f"  Net P&L   : ${r.net_pnl:+.2f}")
    print(f"  Max DD    : {r.max_drawdown_pct:.1%}")
    print(f"  Score     : {r.score:.4f}")
