# Design Note: Cascade Buy-Pause (A) + POC-Aware Grid Spacing (B)

**Status:** Spec A **A_tuned LIVE** enabled (Nasir accepted 2026-09-23 modest cycles/net haircut for dump cut). `SPOT_CASCADE_LIVE` default ON; `SPOT_CASCADE_SHADOW` kept for ops logs. Buys-only pause; sells/fee-proof/MNT/sticky floors unchanged. Do **not** use highbar/flash_cont as live policy.
**Date:** 2026-09-23 (Africa/Lagos)  
**Authority:** Nasir: improve engine without reducing cycles/profit; do NOT enable live pause unless fuller backtest cycles≥baseline (or ≥99%) AND net≥baseline AND buy-into-dump down.  
**Scope order:** Implement/eval **A first**, then **B**. Phase C out of this backtest. **No B/POC implementation now.**  
**Spec A threshold revision + sweep (same day):** Smoke mid-tier A_tuned (`−1.4%` close OR `−2.0%` wick, 90m) cut dump buys but **failed** fuller Top-8 ~21d (40→35 cycles, +$63.77→+$54.53). Stricter/shorter grid also **no winner**. Shadow logger uses A_tuned obs thresholds; `SPOT_CASCADE_LIVE` default ON and wired to cancel_buys_only / build+place gates / DCA skip.

---

## 1. Goal / Non-goals

### Goal
Reduce buy-into-dump inventory (falling knives / cascade impulses) and bias new grid buys toward high-liquidity volume nodes — **without** weakening fee-proof exits or cycle economics vs current `main`.

### Non-goals
- No discretionary order-flow (CVD / footprint / spoof detection) for v1.
- No leverage, no futures logic, no live Jev influence on orders.
- No VAH-clustered take-profits in this plan (Phase C / RANGE-only later).
- No Railway deploy, no production config flip from this doc alone.
- Do not invent new sell-below-buy paths or undercut sticky / hist floors.

---

## 2. Hard constraints (Spot rules — inviolable)

| Rule | Where enforced today |
|------|----------------------|
| Limit-only (no market buys/sells for grid) | `grid_engine.py` place path |
| Never sell below buy / bag-wide FIFO max | `sell_guard.resolve_sell_cost_ref`, `resting_sell_is_safe` |
| Fee-proof + **$0.50 `HARD_MIN_NET_USD`** | `sell_guard.py` |
| Hist cost is **raise-only** (never undercut live bag) | `sell_guard.resolve_sell_cost_ref` |
| Sticky post-buy avg floor is **raise-only** while holding | `grid_engine.py` `_last_portfolio_avg_cost` |
| **MNT hold-only** (never buy more / never grid-sell) | `asset_guards.py` |
| Unlock / mission buy-pauses keep **resting sells** | `unlock_calendar.py`, `mission_tia_recovery.py` |
| Jev stays **Wait / advisory shadow** only | `jev_shadow.py` (`SPOT_JEV_SHADOW`) |

Any A/B change that places a sell below fee-proof(FIFO max)+HARD_MIN_NET, or cancels fee-proof resting sells to “get out,” is an automatic fail.

---

## 3. Current hooks (what exists — cite paths)

### BTC / regime / ADX
- **`trading_engine/spot/btc_master_filter.py`** — in-memory BTC Guard  
  - Flash dump: BTC **−1.5%/1h OR −3.0%/4h** → alt buys frozen **`_flash_dump_cooldown_sec = 1800` (30 min)**  
  - Severe bear: regime BEAR + ADX>30 + 24h < −4% → alt buys paused  
  - ADX/SMA50/200 regime labels; `get_spacing_multiplier()` → **1.40×** spacing in BEAR  
  - Wired at place-time in `grid_engine.py` via `btc_master_filter.is_safe_for_alt_buys`
- **`trading_engine/spot/regime_detector.py`** — BULL / RANGE / BEAR + ADX (1h)
- **`trading_engine/spot/grid_engine.py` `REGIME_PARAMS`** — BEAR already **`buy_levels=0`** (sell-only knife protection)

### Buy pauses / sold-only / unlock
- **`trading_engine/spot/entry_guards.py`**  
  - Soft **dump brake**: ≥ **4.5%** drop from 6h peak + volume ≥ 1.6× median → pause **`DUMP_PAUSE_HOURS = 6`**  
  - **4h trend veto**: price < SMA50, −DI>+DI, ADX≥22  
  - Exitability gate (fee-proof TP reachable)
- **`trading_engine/spot/crash_guard.py`** — rare crash halt: BTC 24h ≤ **−15%** OR equity DD ≥ **25%** (clears −8% / 15%)
- **`trading_engine/spot/unlock_calendar.py`** — ARB hard buy-pause through **2026-09-28 00:00 UTC**; TIA hard pause through **2026-09-26** then ±`UNLOCK_PAD_DAYS` around month-end unlocks; **sells kept**
- **`trading_engine/spot/runner.py`** — Top-8 vs demoted **sell-only**, 10% exposure ceiling → `buy_levels=0`, crash/unlock locks, MNT skip
- **`trading_engine/spot/dca_manager.py`** — anti-falling-knife: suppress DCA in confirmed BEAR

### Grid spacing
- **`grid_engine.py` `build_grid`** — ATR-expanded spacing (≈0.80%–2.50%), weekend / regime scales, BTC spacing multiplier on dip ladder
- Optimizers: `auto_optimizer.py`, `fast_optimizer.py` (spacing search; **not** VP-aware)

### Volume profile
- **None found** under `trading_engine/spot/` (no POC / VAL / VAH / VP helpers). Feature B is net-new math + spacing policy.

### Fee-proof / floors
- **`trading_engine/spot/sell_guard.py`** — `HARD_MIN_NET_USD = 0.50`, fee-proof helpers, hist raise-only
- Sticky floor comments + raise-only write in **`grid_engine.py`**

### Jev
- **`trading_engine/spot/jev_shadow.py`** — shadow/advisory logger only; must not gate live buys for A/B rollout

---

## 4. Spec A — Cascade / falling-knife buy pause

### Intent
Pause or delay **new grid buys** on a sharp BTC (and optionally correlated alt) downside **cascade**, while **keeping fee-proof resting sells** (and normal cancel/replace of unsafe sells only via existing sell_guard).

Fit **existing** `btc_guard` + dump brake + crash halt + ADX/regime — do **not** require CVD/footprint for v1.

### Gap vs today
| Layer | Today | Gap for cascade days (e.g. 2026-09-23) |
|-------|--------|----------------------------------------|
| Flash dump | −1.5%/1h or −3%/4h, **30m** alt freeze | Short; may re-arm buys mid-cascade |
| Dump brake | Per-symbol 4.5%/6h + vol, **6h** | Alt-local; may miss BTC-led cascade before alt prints 4.5% |
| Crash halt | −15% BTC 24h / 25% equity DD | Too rare for “knife” days that still bag inventories |
| BEAR sell-only | Regime latch | Slow vs multi-hour impulse |

**A = mid-tier cascade latch** between flash (30m) and crash (−15%), reusable by runner/grid place path.

### Proposed signals (tunable — marked ★)

**Primary (BTC cascade) — required for v1 (smoke-informed mid-tier, 2026-09-23)**

> **Smoke finding:** Draft Spec A exact (`−2.5%/1h` close **or** `−5%/4h`) was a **no-op** on Bybit 1h Sep 16–23. Dump day 2026-09-23 worst bar: close **−1.47%**, open→low **−2.26%** — never reached −2.5/−5. Mid-tier below still sits **above crash halt (−15% 24h)** and next to flash (−1.5%/1h, −3%/4h, 30m).

1. ★ `BTC_CASCADE_1H_PCT <= -1.4` (close-to-close; mid between flash −1.5 trigger band and the too-strict −2.5 draft), **or**
2. ★ `BTC_CASCADE_WICK_OL_PCT <= -2.0` (open→low on the same 1h bar — catches Sep-23-class wicks that closes understate), **or**
3. ★ `BTC_CASCADE_4H_PCT <= -5.0` (kept as rare severe cascade; flash already covers −3%/4h), **or**
4. ★ Cascade chain: flash dump already active **and** subsequent 1h close still ≤ ★ `−1.0%` (extension while impulse continues)

**Optional alt confirm (default on for Top-8 alts, off for BTC itself)**
- ★ Symbol 1h drop from local 3–6h high ≥ `2.0%` **while** BTC cascade active → same pause bucket  
- Does **not** invent volume-profile or CVD; may reuse dump-brake volume check as soft confirm only

**Regime / ADX interaction**
- If BTC regime already BEAR with ADX≥★25: prefer cascade pause over placing new buys even if flash cooldown expired  
- Never weaken BEAR `buy_levels=0` — A only adds pause when buys would otherwise be allowed (RANGE/BULL)

### Pause behavior
| Still runs | Paused |
|------------|--------|
| Resting fee-proof sells / TP maintenance | New grid **buy** builds & places |
| Sell-guard cancel/replace of under-floor sells | New DCA / mission average-downs that place buys |
| Unlock / exposure / MNT / roster sell-only locks | — |
| Crash halt (superset) | — |

★ **Pause duration (starting values)**
- Base latch: **90 minutes** from last cascade trigger (`CASCADE_PAUSE_MIN = 90`)
- Retrigger extends (max of remaining, new window) — no flicker
- Clear only when: latch expired **and** BTC 1h return > ★ `−0.5%` **and** not in flash-dump freeze  
- Cap: cascade latch does **not** extend crash-halt thresholds (crash stays independent)

### Interaction with other pauses
- **OR** with: flash dump, dump brake, 4h trend veto, crash halt, unlock (ARB/TIA), 10% ceiling, demoted sell-only, MNT never-buy  
- **TIA / ARB unlock buy-pause** remains authoritative for those symbols; cascade is additive, not a bypass  
- **TIA mission** (`mission_tia_recovery.py`): sleeve currently `SLEEVE_USD = 0.0` (disabled); if re-armed later, mission buys remain subject to cascade + crash + dump brake (same as today)  
- **BTC/USDT native grid:** today BTC is exempt from `is_safe_for_alt_buys`. Spec A ★ **applies cascade pause to BTC buys too** (configurable `CASCADE_APPLIES_TO_BTC = True`) — knife risk is highest on the impulse asset

### Implementation sketch (not doing now)
- New small helper e.g. `trading_engine/spot/cascade_guard.py` **or** extend `btc_master_filter` state with `is_cascade_pause` / `cascade_until`  
- Call sites: same as flash — `runner` tick + `grid_engine.place_grid_orders` buy branch  
- Log-only / shadow flag first: `SPOT_CASCADE_SHADOW=1` → log would-block, still place (see Rollout)

### Logging metrics for A
- `cascade_triggers`, `buys_blocked_by_cascade`, `buys_into_dump_baseline_vs_A` (buys within ★60m of local high→low impulse)

---

## 5. Spec B — Volume Profile / POC-aware spacing (after A)

### Intent
Denser grid **near POC / inside value area**; **wider or skip buys below VAL** until price re-enters VA.  
**POC never overrides** FIFO / sticky / fee-proof sell floor.

### Definitions (v1 — OHLCV only, no footprint)
- Window ★: rolling **24h** primary; **48h** sensitivity check in backtest  
- Source: 1h (or 15m if cheap) OHLCV volume histogram  
- Price bins ★: `bin_size = max(tick, ATR_14 * 0.05)` or fixed ★ `0.10%` of mid  
- **POC** = bin with max volume in window  
- **Value area** = contiguous bins around POC covering ★ **70%** of window volume → **VAL** (low), **VAH** (high)

### Spacing rules (buys only)
| Zone | Buy spacing / policy |
|------|----------------------|
| Inside VA, near POC (★ within 0.5× ATR of POC) | ★ `spacing_mult = 0.70` (denser) |
| Inside VA, away from POC | ★ `spacing_mult = 1.00` (baseline regime/ATR) |
| Below VAL | ★ `spacing_mult = 1.50` **or** skip new buy levels until close back ≥ VAL (prefer **skip** for v1 smoke; compare both in backtest) |
| Above VAH | no special densify for buys; **no VAH TP clustering** (Phase C) |

### Sell floor invariant
- All sell targets still: `max(fee_proof(FIFO_max, sticky, hist_raise_only), …) + HARD_MIN_NET`  
- POC / VAH **must not** lower a resting sell  
- If denser buys raise sticky/FIFO floors, sells only **raise** — never cut

### Phase C (optional, out of this backtest)
- VAH-biased TP clustering in **RANGE only** — mention only; do not score A/B on it

---

## 6. Success metrics (live bar = flat-or-better vs baseline)

Compare **baseline = current `main` Spot grid** vs **A alone** vs **A+B** on the same dataset.

| Metric | Gate |
|--------|------|
| **Net USD after fees** | ≥ baseline (flat-or-better) |
| **Completed cycles count** | ≥ ★ 90% of baseline (avoid pause that kills RANGE velocity) |
| **Max bag underwater** (mark vs fee-proof floor, peak USD) | ≤ baseline |
| **Buy-into-dump count** (new buys in ★60m after ≥3% local dump) | **strictly <** baseline |
| Fee-proof violations / sells below floor | **must stay 0** |
| MNT / unlock / hist-raise regressions | **must stay 0** |

**Live consideration rule:** all gates pass on holdout + dump day; Nasir approves; then Railway. Any single gate fail → no deploy.

---

## 7. Backtest plan

### Harness available on Mac
| Path | Role |
|------|------|
| `trading_engine/spot/backtest.py` | Light GridEngine 1h replay (`run_backtest(symbol, days, regime, alloc)`) — **prefer for smoke** |
| `trading_engine/spot/auto_optimizer.py` / `fast_optimizer.py` | Param search wrappers over same engine — **avoid full grid search** if Cursor weekly usage is tight |
| `trading_engine/backtest/engine.py` + `run_synthetic_backtest.py` | Broader engine harness (not Spot-grid-primary) |
| Existing result JSONs | `trading_engine/spot/backtest_compare_*.json`, `backtest_cycle_hunt.json`, … |

**Usage preference:** keep backtests **light** — smoke first, fuller run only if smoke passes. No optimizer sweeps for A/B unless Nasir asks.

### Datasets / symbols / period
- **Symbols (smoke):** `BTC/USDT`, `ETH/USDT`, + 1–2 active Top-8 alts (e.g. whatever roster holds; include `TIA/USDT` only to confirm unlock pause still blocks buys)  
- **Period (fuller):** ≥ 30–60 days ending **2026-09-23**, **must include 2026-09-23 BTC dump day** as a named slice  
- **Dump-day slice:** calendar **2026-09-23 UTC** (±6h pad) — report buy-into-dump + underwater separately  
- Data: Bybit 1h OHLCV via existing `ccxt` fetch in `spot/backtest.py` (cache to `data/` if re-run)

### Variants
1. **Baseline** — current main (no A/B flags)  
2. **A alone** — cascade pause on  
3. **A+B** — cascade + VP spacing (24h VA; skip-below-VAL vs widen-below-VAL as two cheap A/B sub-runs only if smoke OK)

### Pass / fail
- Smoke (1–3 symbols × ~14–30d, RANGE forced + free regime): completes without exception; metrics JSON written under e.g. `trading_engine/spot/backtest_results/` (local, untracked OK)  
- Fuller: gates in §6 on full window **and** dump-day slice  
- Fail if net USD worse, or buy-into-dump not improved, or any floor violation

### Suggested smoke command (illustrative — not run by this note)
```bash
cd /Users/nasir.noma/claude_projects/AIOS
python -m trading_engine.spot.backtest   # existing __main__: BTC/ETH/SOL × regimes, 30d
# Later: thin wrapper with --cascade --poc --since 2026-08-01 --dump-day 2026-09-23
```

---

## 8. Rollout

1. **Shadow / log-only** on Mac or Railway env flag (`SPOT_CASCADE_SHADOW=1`, later `SPOT_POC_SPACING_SHADOW=1`) — no behavior change  
2. Nasir reviews logs + backtest gates  
3. **Nasir approve** → enable real pause/spacing on Railway  
4. **Jev stays Wait/advisory** (`jev_shadow` only) — never wire Choice into buy gate for A/B  
5. Kill-switch: env off reverts to baseline hooks (flash/dump/crash/unlock unchanged)

---

## 9. Out of scope
- Discretionary order-flow / CVD / footprint  
- Leverage / perps  
- Live Jev influence on orders  
- VAH TP clustering (Phase C)  
- Railway deploy from this draft  
- Widening hist or sticky floors downward  
- Selling MNT / buying more MNT  

---

## 10. Open questions for Nasir (review-time)
1. Confirm ★ cascade thresholds (smoke mid-tier: **−1.4%/1h close and/or −2.0% open→low**, 90m latch; −2.5/−5 retired as too strict for 2026-09-23) vs only extending existing flash to 90m.  
2. BTC native buys: apply cascade pause? (spec default **yes**).  
3. Below VAL: **skip buys** vs **widen only** for v1.  
4. VP window 24h vs 48h as primary.

---

## 11. Fuller sweep results (2026-09-23 Lagos) — no live winner

Harness: `trading_engine/spot/smoke_cascade_a_backtest.py --sweep`  
Window: Top-8 × ~21d ending 2026-09-23 UTC; fee 0.1%; RANGE; $3k/symbol; paper `cancel_buys_only` during pause.  
Artifact: `trading_engine/spot/backtest_results/fuller_cascade_sweep_20260923.json`

| mode | cycles | net USD | buy-into-dump | vs baseline |
|------|-------:|--------:|--------------:|-------------|
| **baseline** | **40** | **+63.77** | **51** | — |
| A_tuned (OR −1.4/−2.0, 90m) | 35 | +54.53 | 15 | dump↓; cycles/net FAIL |
| dual_and_90/60/45 (AND −1.4 & −2.0) | 35 | +54.53 | 23 | dump↓; cycles/net FAIL |
| or_short_45/30 (OR −1.4/−2.0) | 35 | +54.53 | 15 | dump↓; cycles/net FAIL |
| highbar_or_90/60 (OR −1.8/−2.5) | 37 | +58.80 | 28 | dump↓; cycles 92.5% & net FAIL |
| flash_cont_30/60 | 37 | +58.80 | 36 | dump↓ slight; cycles/net FAIL |

Sep23 dump-slice: variants that fire on −1.47%/−2.26% wick cut dump buys 12→0 but also cut cycles/net; highbar/flash_cont did **not** improve Sep23 dump buys (12→12).

**Winner:** NONE. Hard constraint → **do not enable live buy-cancel pause.**

### What was implemented (ops improvement, cannot cut cycles/profit)
- `trading_engine/spot/cascade_guard.py` — latch + counters; `SPOT_CASCADE_SHADOW=1` JSONL would-pause logs; `SPOT_CASCADE_LIVE` default OFF and **not** wired to cancel/place
- `btc_master_filter.update` feeds open→low + returns into cascade_guard
- `runner` tick: shadow debug line only; warns if LIVE set (orders still unchanged)
- Status: `btc_guard.summary()["cascade"]`

**Ready-to-deploy shadow:** set `SPOT_CASCADE_SHADOW=1` on Railway when parent approves; set `SPOT_CASCADE_LIVE=1` (default ON) and keep `SPOT_CASCADE_SHADOW=1`. `railway up` deploys LIVE A_tuned buy-pause.

*End of draft. Prefer leave this file **untracked** until Nasir accepts.*
