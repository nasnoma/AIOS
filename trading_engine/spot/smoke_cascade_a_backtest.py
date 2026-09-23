"""
Cascade Spec A backtest harness (smoke + fuller + stricter/shorter sweep).

BACKTEST-ONLY simulation — does NOT wire cascade into live runner/grid_engine
unless a separate live/shadow module is added after gates pass.
Uses existing GridEngine paper fill model + cancel_buys_only during pause windows
derived from BTC 1h OHLCV (same Bybit fetch as spot/backtest.py).

Modes:
  baseline           — current behavior (no cascade)
  A_tuned            — OR close≤−1.4% / wick≤−2.0% / 4h≤−5% / flash-chain; 90m (prior fail)
  dual_and_90        — AND close≤−1.4% AND wick≤−2.0% (+4h/−5 + chain); 90m
  dual_and_60        — same AND; 60m
  dual_and_45        — same AND; 45m
  or_short_45        — same OR as A_tuned; 45m latch
  or_short_30        — same OR as A_tuned; 30m latch
  highbar_or_90      — OR close≤−1.8% OR wick≤−2.5% (+4h/−5 + chain); 90m
  highbar_or_60      — same high-bar OR; 60m
  flash_cont_30      — flash (−1.5%/1h or −3%/4h) + next bar still weak (r1≤−1%); 30m latch
  flash_cont_60      — same flash-continuation; 60m
  A_spec / A_wick    — legacy probes (smoke only)

Sell-side untouched. Paper cancel_buys_only during pause.
Jev is not consulted. No Railway / no push from this harness.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import ccxt
import pandas as pd
import pandas_ta as ta
from loguru import logger

from trading_engine.spot.grid_engine import GridEngine

# Quieter smoke runs
logger.remove()
logger.add(lambda msg: None)


# --- Spec A thresholds / variant grid ---
BTC_CASCADE_4H_PCT = -5.0
CASCADE_CHAIN_1H_PCT = -1.0
FLASH_1H_PCT = -1.5
FLASH_4H_PCT = -3.0
FLASH_PAUSE_MIN = 30
CLEAR_1H_PCT = -0.5
# Legacy probes (A_spec / A_wick only)
LEGACY_CASCADE_1H_PCT = -2.5
LEGACY_WICK_OPEN_LOW_PCT = -2.5

# Buy-into-dump definition for metrics (★ ~60m / sharp BTC down hour)
DUMP_BUY_BTC_1H_PCT = -1.0
DUMP_BUY_BTC_OL_PCT = -1.5

# Default A_tuned (compat aliases used by meta JSON)
BTC_CASCADE_1H_PCT = -1.4
BTC_CASCADE_WICK_OL_PCT = -2.0
CASCADE_PAUSE_MIN = 90

# Parameterized mode grid for fuller sweep
MODE_CONFIGS = {
    "A_tuned": {
        "logic": "or",
        "close_pct": -1.4,
        "wick_pct": -2.0,
        "use_4h": True,
        "use_chain": True,
        "flash_cont_only": False,
        "pause_min": 90,
    },
    "dual_and_90": {
        "logic": "and",
        "close_pct": -1.4,
        "wick_pct": -2.0,
        "use_4h": True,
        "use_chain": True,
        "flash_cont_only": False,
        "pause_min": 90,
    },
    "dual_and_60": {
        "logic": "and",
        "close_pct": -1.4,
        "wick_pct": -2.0,
        "use_4h": True,
        "use_chain": True,
        "flash_cont_only": False,
        "pause_min": 60,
    },
    "dual_and_45": {
        "logic": "and",
        "close_pct": -1.4,
        "wick_pct": -2.0,
        "use_4h": True,
        "use_chain": True,
        "flash_cont_only": False,
        "pause_min": 45,
    },
    "or_short_45": {
        "logic": "or",
        "close_pct": -1.4,
        "wick_pct": -2.0,
        "use_4h": True,
        "use_chain": True,
        "flash_cont_only": False,
        "pause_min": 45,
    },
    "or_short_30": {
        "logic": "or",
        "close_pct": -1.4,
        "wick_pct": -2.0,
        "use_4h": True,
        "use_chain": True,
        "flash_cont_only": False,
        "pause_min": 30,
    },
    "highbar_or_90": {
        "logic": "or",
        "close_pct": -1.8,
        "wick_pct": -2.5,
        "use_4h": True,
        "use_chain": True,
        "flash_cont_only": False,
        "pause_min": 90,
    },
    "highbar_or_60": {
        "logic": "or",
        "close_pct": -1.8,
        "wick_pct": -2.5,
        "use_4h": True,
        "use_chain": True,
        "flash_cont_only": False,
        "pause_min": 60,
    },
    "flash_cont_30": {
        "logic": "flash_cont",
        "close_pct": None,
        "wick_pct": None,
        "use_4h": False,
        "use_chain": False,
        "flash_cont_only": True,
        "pause_min": 30,
        "cont_r1_pct": -1.0,
    },
    "flash_cont_60": {
        "logic": "flash_cont",
        "close_pct": None,
        "wick_pct": None,
        "use_4h": False,
        "use_chain": False,
        "flash_cont_only": True,
        "pause_min": 60,
        "cont_r1_pct": -1.0,
    },
    # Legacy smoke probes
    "A_spec": {
        "logic": "legacy_spec",
        "close_pct": -2.5,
        "wick_pct": None,
        "use_4h": True,
        "use_chain": True,
        "flash_cont_only": False,
        "pause_min": 90,
    },
    "A_wick": {
        "logic": "legacy_wick",
        "close_pct": -2.5,
        "wick_pct": -2.5,
        "use_4h": True,
        "use_chain": True,
        "flash_cont_only": False,
        "pause_min": 90,
    },
}

SWEEP_MODES = [
    "baseline",
    "A_tuned",
    "dual_and_90",
    "dual_and_60",
    "dual_and_45",
    "or_short_45",
    "or_short_30",
    "highbar_or_90",
    "highbar_or_60",
    "flash_cont_30",
    "flash_cont_60",
]


class DummyPortfolio:
    def __init__(self):
        self.positions = {}

    def get_position(self, symbol):
        return self.positions.get(symbol, 0.0)

    def record_buy(self, symbol, qty, price, *args, **kwargs):
        base = symbol.split("/")[0]
        self.positions[base] = self.positions.get(base, 0.0) + qty

    def record_sell(self, symbol, qty, price, *args, **kwargs):
        base = symbol.split("/")[0]
        self.positions[base] = max(0.0, self.positions.get(base, 0.0) - qty)


@dataclass
class CascadeState:
    flash_until: Optional[datetime] = None
    cascade_until: Optional[datetime] = None
    paused: bool = False
    triggers: int = 0
    pause_bars: int = 0
    pause_hour_keys: set = field(default_factory=set)  # "YYYY-MM-DD HH" UTC


def fetch_ohlcv(exchange, symbol: str, since_ms: int) -> pd.DataFrame:
    rows: List[list] = []
    t = since_ms
    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe="1h", since=t, limit=1000)
        if not batch:
            break
        rows.extend(batch)
        t = batch[-1][0] + 1
        if len(batch) < 1000:
            break
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
    df.ta.atr(length=14, append=True)
    return df


def atr_col(df: pd.DataFrame) -> Optional[str]:
    if "ATRr_14" in df.columns:
        return "ATRr_14"
    if "ATR_14" in df.columns:
        return "ATR_14"
    return None


def btc_returns_at(btc: pd.DataFrame, i: int) -> Tuple[float, float, float]:
    """close-to-close 1h/4h and open→low % for bar i."""
    c = float(btc.loc[i, "close"])
    o = float(btc.loc[i, "open"])
    lo = float(btc.loc[i, "low"])
    c1 = float(btc.loc[i - 1, "close"]) if i >= 1 else o
    c4 = float(btc.loc[i - 4, "close"]) if i >= 4 else float(btc.loc[0, "close"])
    r1 = ((c - c1) / c1) * 100.0 if c1 else 0.0
    r4 = ((c - c4) / c4) * 100.0 if c4 else 0.0
    ol = ((lo - o) / o) * 100.0 if o else 0.0
    return r1, r4, ol


def update_cascade(
    state: CascadeState,
    ts: datetime,
    r1: float,
    r4: float,
    ol: float,
    mode: str,
) -> bool:
    """Update latch; return whether buys should be paused this bar."""
    if mode == "baseline":
        state.paused = False
        return False

    cfg = MODE_CONFIGS.get(mode)
    if cfg is None:
        raise ValueError(f"unknown cascade mode: {mode}")

    pause_min = int(cfg.get("pause_min", CASCADE_PAUSE_MIN))

    # Flash (same thresholds as btc_master_filter)
    if r1 < FLASH_1H_PCT or r4 < FLASH_4H_PCT:
        cand = ts + timedelta(minutes=FLASH_PAUSE_MIN)
        state.flash_until = max(state.flash_until, cand) if state.flash_until else cand
    flash_active = bool(state.flash_until and ts < state.flash_until)

    triggered = False
    logic = cfg["logic"]
    close_pct = cfg.get("close_pct")
    wick_pct = cfg.get("wick_pct")
    cont_r1 = float(cfg.get("cont_r1_pct", CASCADE_CHAIN_1H_PCT))

    if logic == "flash_cont":
        # Flash-continuation only: existing flash signal + this bar still weak
        if flash_active and r1 <= cont_r1:
            triggered = True
    elif logic == "and":
        primary = (close_pct is not None and r1 <= close_pct) and (wick_pct is not None and ol <= wick_pct)
        if primary:
            triggered = True
        elif cfg.get("use_4h") and r4 <= BTC_CASCADE_4H_PCT:
            triggered = True
        elif cfg.get("use_chain") and flash_active and r1 <= CASCADE_CHAIN_1H_PCT:
            triggered = True
    elif logic == "or":
        if close_pct is not None and r1 <= close_pct:
            triggered = True
        elif wick_pct is not None and ol <= wick_pct:
            triggered = True
        elif cfg.get("use_4h") and r4 <= BTC_CASCADE_4H_PCT:
            triggered = True
        elif cfg.get("use_chain") and flash_active and r1 <= CASCADE_CHAIN_1H_PCT:
            triggered = True
    elif logic == "legacy_spec":
        if r1 <= LEGACY_CASCADE_1H_PCT or r4 <= BTC_CASCADE_4H_PCT:
            triggered = True
        elif flash_active and r1 <= CASCADE_CHAIN_1H_PCT:
            triggered = True
    elif logic == "legacy_wick":
        if r1 <= LEGACY_CASCADE_1H_PCT or r4 <= BTC_CASCADE_4H_PCT:
            triggered = True
        elif flash_active and r1 <= CASCADE_CHAIN_1H_PCT:
            triggered = True
        if wick_pct is not None and ol <= wick_pct:
            triggered = True
    else:
        raise ValueError(f"unknown cascade logic: {logic}")

    if triggered:
        state.triggers += 1
        cand = ts + timedelta(minutes=pause_min)
        state.cascade_until = max(state.cascade_until, cand) if state.cascade_until else cand

    latch_active = bool(state.cascade_until and ts < state.cascade_until)
    if latch_active:
        state.paused = True
    elif state.cascade_until is not None:
        # Latch expired: clear only if 1h > CLEAR and not flash
        if r1 > CLEAR_1H_PCT and not flash_active:
            state.cascade_until = None
            state.paused = False
        else:
            state.paused = True  # hold until clear conditions
    else:
        state.paused = False

    if state.paused:
        state.pause_bars += 1
        state.pause_hour_keys.add(ts.strftime("%Y-%m-%d %H"))
    return state.paused


def is_dump_hour(r1: float, ol: float) -> bool:
    return r1 <= DUMP_BUY_BTC_1H_PCT or ol <= DUMP_BUY_BTC_OL_PCT


def run_one(
    symbol: str,
    df: pd.DataFrame,
    btc: pd.DataFrame,
    btc_ts_to_i: Dict[Any, int],
    regime: str,
    mode: str,
    allocated_usd: float,
    fee_rate: float,
) -> Dict[str, Any]:
    acol = atr_col(df)
    engine = GridEngine(
        symbol=symbol,
        allocated_usd=allocated_usd,
        paper_mode=True,
        fee_rate=fee_rate,
    )
    engine.set_regime(regime)
    portfolio = DummyPortfolio()

    start_price = float(df.iloc[0]["open"])
    init_atr = float(df[acol].iloc[14]) if acol and len(df) > 14 and pd.notna(df[acol].iloc[14]) else 0.0
    engine.build_grid(start_price, portfolio, atr=init_atr)
    engine.place_grid_orders(portfolio, exchange=None)

    last_rebuild = df.iloc[0]["timestamp"]
    cascade = CascadeState()
    total_buys = 0
    total_sells = 0
    buys_into_dump = 0
    buys_blocked = 0  # bars where pause prevented an otherwise-touching buy (approx)
    buy_events: List[Dict[str, Any]] = []

    # Inventory for underwater
    inv_qty = 0.0
    inv_cost = 0.0
    max_underwater_usd = 0.0
    peak_capital = allocated_usd
    max_dd_realized = 0.0

    # Align BTC index by timestamp
    for idx, row in df.iterrows():
        ts = row["timestamp"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        bi = btc_ts_to_i.get(pd.Timestamp(ts))
        if bi is None:
            # nearest prior BTC bar
            prior = btc[btc["timestamp"] <= ts]
            bi = int(prior.index[-1]) if len(prior) else 0
        r1, r4, ol = btc_returns_at(btc, bi)
        paused = update_cascade(cascade, ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts, r1, r4, ol, mode)

        # Daily rebuild
        if (row["timestamp"] - last_rebuild).total_seconds() >= 86400:
            row_atr = float(df.loc[idx, acol]) if acol and pd.notna(df.loc[idx, acol]) else 0.0
            engine.cancel_all()
            engine.build_grid(float(row["close"]), portfolio, atr=row_atr)
            engine.place_grid_orders(portfolio, exchange=None)
            last_rebuild = row["timestamp"]

        if paused:
            # Spec A: cancel new/resting buys; sells continue
            open_buys_before = sum(1 for l in engine.grid_levels if l.status == "open" and l.side == "buy")
            if open_buys_before:
                # Would-touch buys this hour (approx blocked)
                low = float(row["low"])
                would = sum(
                    1
                    for l in engine.grid_levels
                    if l.status == "open" and l.side == "buy" and low <= l.price
                )
                buys_blocked += would
                engine.cancel_buys_only()

        # Buy fills only when not paused
        if not paused:
            fills = engine.tick(float(row["low"]), portfolio)
            n_buy = sum(1 for f in fills if f["side"] == "buy")
            total_buys += n_buy
            for f in fills:
                if f["side"] != "buy":
                    continue
                buy_events.append({"ts": str(ts), "price": f["price"], "qty": f["qty"], "r1": r1, "ol": ol})
                if is_dump_hour(r1, ol):
                    buys_into_dump += 1
                inv_qty += float(f["qty"])
                inv_cost += float(f["qty"]) * float(f["price"])

        # Sells always
        fills = engine.tick(float(row["high"]), portfolio)
        n_sell = sum(1 for f in fills if f["side"] == "sell")
        total_sells += n_sell
        for f in fills:
            if f["side"] != "sell":
                continue
            q = float(f["qty"])
            if inv_qty > 1e-12:
                avg = inv_cost / inv_qty
                take = min(q, inv_qty)
                inv_cost -= avg * take
                inv_qty -= take
                if inv_qty < 1e-12:
                    inv_qty = 0.0
                    inv_cost = 0.0

        mark = float(row["close"])
        if inv_qty > 0 and inv_cost > 0:
            avg = inv_cost / inv_qty
            uw = (avg - mark) * inv_qty
            if uw > max_underwater_usd:
                max_underwater_usd = uw

        pnl = sum(c["net_pnl"] for c in engine.completed_cycles)
        cur = allocated_usd + pnl
        if cur > peak_capital:
            peak_capital = cur
        dd = peak_capital - cur
        if dd > max_dd_realized:
            max_dd_realized = dd

    net = sum(c["net_pnl"] for c in engine.completed_cycles)
    gross = sum(c["gross_pnl"] for c in engine.completed_cycles)
    fees = sum(c["fee"] for c in engine.completed_cycles)
    end_price = float(df.iloc[-1]["close"])
    days = max(1e-9, (df.iloc[-1]["timestamp"] - df.iloc[0]["timestamp"]).total_seconds() / 86400.0)

    return {
        "symbol": symbol,
        "mode": mode,
        "regime": regime,
        "days": round(days, 2),
        "bars": len(df),
        "total_cycles": len(engine.completed_cycles),
        "total_buys": total_buys,
        "total_sells": total_sells,
        "buys_into_dump": buys_into_dump,
        "buys_blocked_est": buys_blocked,
        "cascade_triggers": cascade.triggers,
        "cascade_pause_bars": cascade.pause_bars,
        "cascade_pause_hours": len(cascade.pause_hour_keys),
        "cascade_pause_days": len({h[:10] for h in cascade.pause_hour_keys}),
        "gross_pnl_usd": float(gross),
        "fees_paid_usd": float(fees),
        "net_pnl_usd": float(net),
        "max_underwater_usd": float(max_underwater_usd),
        "max_drawdown_realized_usd": float(max_dd_realized),
        "start_price": start_price,
        "end_price": end_price,
        "price_change_pct": float(((end_price - start_price) / start_price) * 100) if start_price else 0.0,
        "buy_events_sample": buy_events[:20],
    }


def dump_day_slice(df: pd.DataFrame, day: str = "2026-09-23") -> pd.DataFrame:
    d0 = pd.Timestamp(f"{day} 00:00:00", tz="UTC")
    d1 = d0 + timedelta(hours=30)  # +6h pad into next day
    d_start = d0 - timedelta(hours=6)
    return df[(df["timestamp"] >= d_start) & (df["timestamp"] < d1)].reset_index(drop=True)


def aggregate(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    keys = [
        "total_cycles",
        "total_buys",
        "total_sells",
        "buys_into_dump",
        "buys_blocked_est",
        "cascade_triggers",
        "cascade_pause_bars",
        "cascade_pause_hours",
        "cascade_pause_days",
        "gross_pnl_usd",
        "fees_paid_usd",
        "net_pnl_usd",
        "max_underwater_usd",
        "max_drawdown_realized_usd",
    ]
    out = {k: 0.0 if "usd" in k or "pnl" in k else 0 for k in keys}
    out["max_underwater_usd"] = 0.0
    out["max_drawdown_realized_usd"] = 0.0
    for r in rows:
        for k in keys:
            if k.startswith("max_") or k in ("cascade_pause_hours", "cascade_pause_days"):
                # BTC-driven latch is shared; hours/days are calendar measures → max
                out[k] = max(float(out[k]) if isinstance(out[k], float) else int(out[k]), float(r.get(k, 0) or 0) if "usd" in k or "pnl" in k or k.startswith("max_") else int(r.get(k, 0) or 0))
                if k in ("cascade_pause_hours", "cascade_pause_days"):
                    out[k] = int(out[k])
            else:
                out[k] = type(out[k])(out[k] + r.get(k, 0))
    return out


def gate_eval(base: Dict[str, Any], var: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate gates. Hard live-enable prefs: net>=base, cycles>=base (prefer 100%), dump down.
    all_hard_gates uses cycles>=95% floor (Nasir allow if none hit 100%); prefer_live uses 100%.
    """
    cycles_ratio = (var["total_cycles"] / base["total_cycles"]) if base["total_cycles"] else (1.0 if var["total_cycles"] == 0 else 0.0)
    cycles_ge_100 = var["total_cycles"] >= base["total_cycles"] if base["total_cycles"] else var["total_cycles"] == 0
    cycles_ge_99 = cycles_ratio >= 0.99 if base["total_cycles"] else cycles_ge_100
    cycles_ge_95 = cycles_ratio >= 0.95 if base["total_cycles"] else cycles_ge_100
    cycles_ge_90 = cycles_ratio >= 0.90 if base["total_cycles"] else cycles_ge_100
    net_ok = var["net_pnl_usd"] >= base["net_pnl_usd"] - 1e-9
    dump_ok = var["buys_into_dump"] < base["buys_into_dump"]
    dump_na = base["buys_into_dump"] == 0
    uw_ok = var["max_underwater_usd"] <= base["max_underwater_usd"] + 1e-9
    dump_pass = bool(dump_ok) if not dump_na else False
    # Prefer 100% cycles; hard floor for "pass candidate" is 95% if none hit 100%
    hard = bool(net_ok and cycles_ge_95 and dump_pass)
    prefer_live = bool(net_ok and cycles_ge_100 and dump_pass)  # strict Nasir preference
    return {
        "net_ge_baseline": {"pass": bool(net_ok), "baseline": base["net_pnl_usd"], "variant": var["net_pnl_usd"]},
        "cycles_ge_100pct": {
            "pass": bool(cycles_ge_100),
            "baseline": base["total_cycles"],
            "variant": var["total_cycles"],
            "ratio": cycles_ratio,
        },
        "cycles_ge_99pct": {"pass": bool(cycles_ge_99), "ratio": cycles_ratio},
        "cycles_ge_95pct": {"pass": bool(cycles_ge_95), "ratio": cycles_ratio},
        "cycles_ge_90pct": {
            "pass": bool(cycles_ge_90),
            "baseline": base["total_cycles"],
            "variant": var["total_cycles"],
            "ratio": cycles_ratio,
        },
        "buy_into_dump_strictly_down": {
            "pass": dump_pass,
            "baseline": base["buys_into_dump"],
            "variant": var["buys_into_dump"],
            "note": "baseline already 0 — strict < impossible" if dump_na else None,
        },
        "max_underwater_le_baseline": {
            "pass": bool(uw_ok),
            "baseline": base["max_underwater_usd"],
            "variant": var["max_underwater_usd"],
        },
        "all_hard_gates": hard,  # net + cycles>=95% + dump down
        "prefer_live_gates": prefer_live,  # net + cycles>=100% + dump down
    }


def main():
    import argparse

    ap = argparse.ArgumentParser(description="Spec A cascade buy-pause backtest (paper-only)")
    ap.add_argument("--fuller", action="store_true", help="Top-8-ish × ~21d ending 2026-09-23; baseline vs A_tuned")
    ap.add_argument("--sweep", action="store_true", help="Fuller window + stricter/shorter variant grid")
    ap.add_argument("--days", type=int, default=None, help="Lookback days ending now/Sep23 (overrides --fuller start)")
    args = ap.parse_args()

    exchange = ccxt.bybit({"enableRateLimit": True})
    fee = 0.001
    regime = "RANGE"
    alloc = 3000.0

    if args.sweep or args.fuller or args.days:
        # Fuller: more symbols, ~3 weeks through dump day (usage-light — no optimizer)
        symbols = [
            "BTC/USDT",
            "ETH/USDT",
            "SOL/USDT",
            "AVAX/USDT",
            "LINK/USDT",
            "SUI/USDT",
            "NEAR/USDT",
            "DOT/USDT",
        ]
        days = args.days or 21
        # End inclusive of 2026-09-23 dump day
        end = datetime(2026, 9, 23, 23, 0, tzinfo=timezone.utc)
        start = end - timedelta(days=days)
        if args.sweep:
            modes = list(SWEEP_MODES)
            out_name = "fuller_cascade_sweep_20260923.json"
            run_label = "sweep"
        else:
            modes = ["baseline", "A_tuned"]
            out_name = "fuller_cascade_a_20260923.json"
            run_label = "fuller"
    else:
        # Original short smoke
        symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
        start = datetime(2026, 9, 16, 0, 0, tzinfo=timezone.utc)
        modes = ["baseline", "A_tuned", "A_spec", "A_wick"]
        out_name = "smoke_cascade_a_20260923.json"
        run_label = "smoke"

    since_ms = int(start.timestamp() * 1000)

    print(f"[{run_label}] Fetching OHLCV since {start.isoformat()} … symbols={symbols} modes={modes}")
    btc = fetch_ohlcv(exchange, "BTC/USDT", since_ms)
    if btc.empty:
        raise SystemExit("No BTC data — blocker")
    # Truncate to <= 2026-09-23 23:00 UTC if exchange returns later bars
    end_cap = pd.Timestamp("2026-09-23 23:00:00", tz="UTC")
    btc = btc[btc["timestamp"] <= end_cap].reset_index(drop=True)
    btc_ts_to_i = {pd.Timestamp(t): i for i, t in enumerate(btc["timestamp"])}
    print(f"BTC bars={len(btc)} {btc.iloc[0]['timestamp']} → {btc.iloc[-1]['timestamp']}")

    # Preflight triggers for A_tuned (+ legacy if in modes)
    preflight = {
        "window_start": str(btc.iloc[0]["timestamp"]),
        "window_end": str(btc.iloc[-1]["timestamp"]),
        "worst_btc_1h_close": None,
        "sep23_worst_ol": None,
    }
    for mode_pf in [m for m in modes if m != "baseline"]:
        st = CascadeState()
        for i in range(len(btc)):
            ts = btc.loc[i, "timestamp"].to_pydatetime()
            r1, r4, ol = btc_returns_at(btc, i)
            update_cascade(st, ts, r1, r4, ol, mode_pf)
        preflight[f"{mode_pf}_triggers"] = st.triggers
        preflight[f"{mode_pf}_pause_bars"] = st.pause_bars
        preflight[f"{mode_pf}_pause_hours"] = len(st.pause_hour_keys)
        preflight[f"{mode_pf}_pause_days"] = len({h[:10] for h in st.pause_hour_keys})
    worst = min((btc_returns_at(btc, i)[0], str(btc.loc[i, "timestamp"])) for i in range(1, len(btc)))
    preflight["worst_btc_1h_close"] = {"pct": worst[0], "ts": worst[1]}
    s23 = btc[btc["timestamp"].dt.strftime("%Y-%m-%d") == "2026-09-23"]
    if len(s23):
        w = min(
            (btc_returns_at(btc, i)[2], str(btc.loc[i, "timestamp"]), btc_returns_at(btc, i)[0])
            for i in s23.index
        )
        preflight["sep23_worst_ol"] = {"ol_pct": w[0], "ts": w[1], "r1_pct": w[2]}
    print("PREFLIGHT", json.dumps(preflight, indent=2, default=str))

    data = {}
    for sym in symbols:
        if sym == "BTC/USDT":
            data[sym] = btc.copy()
        else:
            df = fetch_ohlcv(exchange, sym, since_ms)
            if df.empty:
                print(f"WARN: no data for {sym}, skipping")
                continue
            data[sym] = df[df["timestamp"] <= end_cap].reset_index(drop=True)
            print(f"{sym} bars={len(data[sym])}")

    symbols = [s for s in symbols if s in data and not data[s].empty]

    results: Dict[str, Any] = {
        "meta": {
            "created_lagos": datetime.now(timezone.utc).astimezone().isoformat(),
            "run_label": run_label,
            "harness": "backtest-only sim via smoke_cascade_a_backtest.py",
            "engine_wired": False,
            "symbols": symbols,
            "regime": regime,
            "allocated_usd_per_symbol": alloc,
            "fee_rate": fee,
            "thresholds_A_tuned": {
                "BTC_CASCADE_1H_PCT": BTC_CASCADE_1H_PCT,
                "BTC_CASCADE_WICK_OL_PCT": BTC_CASCADE_WICK_OL_PCT,
                "BTC_CASCADE_4H_PCT": BTC_CASCADE_4H_PCT,
                "CASCADE_CHAIN_1H_PCT": CASCADE_CHAIN_1H_PCT,
                "CASCADE_PAUSE_MIN": CASCADE_PAUSE_MIN,
            },
            "mode_configs": {k: v for k, v in MODE_CONFIGS.items() if k in modes or k == "A_tuned"},
            "legacy_A_spec": {
                "LEGACY_CASCADE_1H_PCT": LEGACY_CASCADE_1H_PCT,
                "LEGACY_WICK_OPEN_LOW_PCT": LEGACY_WICK_OPEN_LOW_PCT,
            },
            "preflight": preflight,
            "caveats": [
                "Cascade pause simulated by cancel_buys_only during pause; sells still tick.",
                "Not wired into live btc_master_filter / runner / place_grid_orders — paper GridEngine only.",
                "1h OHLCV; A_tuned uses close≤−1.4% OR open→low≤−2.0% (smoke mid-tier).",
                "Buy-into-dump = buys on bars with BTC 1h close≤−1% or open→low≤−1.5%.",
                "Forced RANGE regime; not live Top-8 rotator / unlock / crash / dump-brake stack.",
                "Jev not used. No Railway.",
            ],
        },
        "full_window": {},
        "dump_day_slice": {},
        "gates_full": {},
        "gates_dump_day": {},
    }

    for mode in modes:
        rows = []
        for sym in symbols:
            df = data[sym]
            r = run_one(sym, df, btc, btc_ts_to_i, regime, mode, alloc, fee)
            rows.append(r)
            print(
                f"{mode:10s} {sym:10s} cycles={r['total_cycles']:4d} net=${r['net_pnl_usd']:+8.2f} "
                f"dump_buys={r['buys_into_dump']} triggers={r['cascade_triggers']} "
                f"pause_bars={r['cascade_pause_bars']} pause_h={r['cascade_pause_hours']} pause_d={r['cascade_pause_days']}"
            )
        results["full_window"][mode] = {"per_symbol": rows, "aggregate": aggregate(rows)}

    for mode in modes:
        rows = []
        for sym in symbols:
            df = dump_day_slice(data[sym])
            if df.empty:
                continue
            start_ts = df.iloc[0]["timestamp"]
            warm = data[sym][data[sym]["timestamp"] < start_ts].tail(40)
            df_run = pd.concat([warm, df], ignore_index=True)
            df_run.ta.atr(length=14, append=True)
            r = run_one(sym, df_run, btc, btc_ts_to_i, regime, mode, alloc, fee)
            r["note"] = "includes ~40h ATR warm-up bars before dump slice; cycles/pnl include warm-up"
            rows.append(r)
            print(
                f"DUMP {mode:10s} {sym:10s} cycles={r['total_cycles']:4d} net=${r['net_pnl_usd']:+8.2f} "
                f"dump_buys={r['buys_into_dump']} triggers={r['cascade_triggers']}"
            )
        results["dump_day_slice"][mode] = {"per_symbol": rows, "aggregate": aggregate(rows)}

    base_f = results["full_window"]["baseline"]["aggregate"]
    base_d = results["dump_day_slice"]["baseline"]["aggregate"]
    for mode in [m for m in modes if m != "baseline"]:
        results["gates_full"][mode] = gate_eval(base_f, results["full_window"][mode]["aggregate"])
        results["gates_dump_day"][mode] = gate_eval(base_d, results["dump_day_slice"][mode]["aggregate"])

    print("GATES_FULL", json.dumps(results["gates_full"], indent=2))
    print("GATES_DUMP", json.dumps(results["gates_dump_day"], indent=2))

    # Compact parent-facing summary + sweep ranking
    print("\n=== VARIANT TABLE (full window) ===")
    print(f"{'mode':16s} {'cycles':>7s} {'net':>10s} {'dump':>6s} {'uw':>10s} {'trig':>5s} {'p_h':>4s} {'c100':>4s} {'c95':>3s} {'netOK':>5s} {'dmp':>3s} {'hard':>4s} {'live':>4s}")
    ranking = []
    for mode in [m for m in modes if m != "baseline"]:
        a = results["full_window"][mode]["aggregate"]
        g = results["gates_full"][mode]
        gd = results["gates_dump_day"].get(mode, {})
        dump_slice_improve = bool(gd.get("buy_into_dump_strictly_down", {}).get("pass"))
        # Also accept dump-slice dump buys <= baseline if baseline dump-slice dump already low but still "improves" via full
        # User: Sep23 dump slice still improves → use dump gate dump-down
        print(
            f"{mode:16s} {a['total_cycles']:7d} {a['net_pnl_usd']:10.2f} {a['buys_into_dump']:6d} "
            f"{a['max_underwater_usd']:10.2f} {a['cascade_triggers']:5d} {a.get('cascade_pause_hours', 0):4d} "
            f"{str(g['cycles_ge_100pct']['pass']):>4s} {str(g['cycles_ge_95pct']['pass']):>3s} "
            f"{str(g['net_ge_baseline']['pass']):>5s} {str(g['buy_into_dump_strictly_down']['pass']):>3s} "
            f"{str(g['all_hard_gates']):>4s} {str(g['prefer_live_gates']):>4s}"
        )
        ranking.append({
            "mode": mode,
            "cycles": a["total_cycles"],
            "net": a["net_pnl_usd"],
            "dump": a["buys_into_dump"],
            "uw": a["max_underwater_usd"],
            "ratio": g["cycles_ge_100pct"]["ratio"],
            "hard": g["all_hard_gates"],
            "prefer_live": g["prefer_live_gates"],
            "dump_slice_improve": dump_slice_improve,
            "dump_slice_dump": results["dump_day_slice"][mode]["aggregate"]["buys_into_dump"],
            "dump_slice_base_dump": base_d["buys_into_dump"],
            "cfg": MODE_CONFIGS.get(mode),
        })

    # Winner: prefer_live + dump_slice_improve; else hard+dump_slice; prefer higher cycles then higher net then lower dump
    def rank_key(r):
        return (
            1 if (r["prefer_live"] and r["dump_slice_improve"]) else 0,
            1 if (r["hard"] and r["dump_slice_improve"]) else 0,
            1 if r["prefer_live"] else 0,
            1 if r["hard"] else 0,
            r["cycles"],
            r["net"],
            -r["dump"],
        )

    ranking.sort(key=rank_key, reverse=True)
    results["ranking"] = ranking
    winners_live = [r for r in ranking if r["prefer_live"] and r["dump_slice_improve"]]
    winners_hard = [r for r in ranking if r["hard"] and r["dump_slice_improve"]]
    if winners_live:
        w = winners_live[0]
        results["winner"] = {"mode": w["mode"], "tier": "prefer_live_100pct", **{k: w[k] for k in ("cycles", "net", "dump", "uw", "ratio", "cfg")}}
        print(f"\nWINNER (prefer live 100% cycles): {w['mode']} cycles={w['cycles']} net={w['net']:.4f} dump={w['dump']}")
    elif winners_hard:
        w = winners_hard[0]
        results["winner"] = {"mode": w["mode"], "tier": "hard_95pct_floor", **{k: w[k] for k in ("cycles", "net", "dump", "uw", "ratio", "cfg")}}
        print(f"\nWINNER (95% cycles floor, no 100% passer): {w['mode']} cycles={w['cycles']} net={w['net']:.4f} dump={w['dump']}")
    else:
        results["winner"] = None
        print("\nWINNER: NONE — no variant passes net>=base AND cycles>=95% AND dump-down (full + Sep23 slice)")

    if "A_tuned" in results["full_window"]:
        a = results["full_window"]["A_tuned"]["aggregate"]
        g = results["gates_full"]["A_tuned"]
        print("\n=== SUMMARY A_tuned vs baseline (full window) ===")
        print(f"cycles: {base_f['total_cycles']} → {a['total_cycles']} ({g['cycles_ge_90pct']['ratio']})")
        print(f"net_usd: {base_f['net_pnl_usd']:.4f} → {a['net_pnl_usd']:.4f}")
        print(f"buy_into_dump: {base_f['buys_into_dump']} → {a['buys_into_dump']}")
        print(f"max_underwater: {base_f['max_underwater_usd']:.4f} → {a['max_underwater_usd']:.4f}")
        print(f"pause_hours/days (agg max): {a.get('cascade_pause_hours')}/{a.get('cascade_pause_days')}")
        print(f"all_hard_gates: {g['all_hard_gates']} prefer_live: {g['prefer_live_gates']}")


    out_path = os.path.join(os.path.dirname(__file__), "backtest_results", out_name)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print("WROTE", out_path)



if __name__ == "__main__":
    main()
