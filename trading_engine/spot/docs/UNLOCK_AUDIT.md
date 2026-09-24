# Unlock calendar coverage audit

**Date:** 2026-09-24 (Africa/Lagos)  
**Scope:** Soft unlock watch + coverage gaps. No book-wide slow-bleed pause. No fee-floor / cascade threshold changes.

## Hard rules (unchanged)

- Hard buy-pause allowlist: **ARB/USDT, TIA/USDT only** (`HARD_PAUSE_ALLOWLIST`)
- New coins: **log-first / advisory** (`AdvisoryUnlock`, `enabled=False` or soft-log only). Do **not** auto-enable hard pause without Nasir confirm + allowlist edit.
- Sells / fee-proof / MNT / hist raise-only floors unchanged.

## In-repo calendar sources

| Source | Path | Notes |
|--------|------|-------|
| Explicit unlocks + ARB/TIA hard floors | `spot/unlock_calendar.py` `_EXPLICIT_UNLOCKS` | Authoritative for hard pause |
| Advisory / shadow rows | `spot/unlock_calendar.py` `_ADVISORY_UNLOCKS` | Empty of invented dates; add well-sourced rows with `hard_pause=False` |
| Design note | `spot/docs/DESIGN_buy_pause_and_poc_grid.md` | Cascade / unlock hooks |

No external TokenUnlocks scrape in this change — audit uses in-repo rows only.

## How to run

```bash
cd /path/to/AIOS
PYTHONPATH=. python trading_engine/scripts/audit_unlock_coverage.py
PYTHONPATH=. python trading_engine/scripts/audit_unlock_coverage.py --json
```

## Snapshot (2026-09-24)

- **Covered explicit:** ARB/USDT, TIA/USDT
- **Next ~30d (explicit):**
  - ARB/USDT 2026-09-23 (hard pause through 2026-09-28 00:00 UTC)
  - TIA/USDT 2026-09-30 (early hard pause through 2026-09-26, then ±4d pad)
- **Gaps vs full Halal/Top-8 universe:** all other live roster symbols (OP, APT, SEI, SUI, NEAR, …) — **no calendar row**. Soft `[UNLOCK WATCH]` logs gaps hourly; does not pause buys.
- **Needs Nasir approve before hard-pause:** any new symbol (add date + `HARD_PAUSE_ALLOWLIST` + set `hard_pause=True` only after confirm). Safer path: `AdvisoryUnlock(..., enabled=True, hard_pause=False)` for soft log only.

## Soft alerts (engine)

| Alert | Threshold (start) | Action |
|-------|-------------------|--------|
| `reserve_near_floor` | free ≤ max(reserve×1.02, reserve+$25) | rate-limited log + status |
| `slow_bleed_watch` | BTC ≤ −8%/72h **or** equity DD from 72h peak ≥ 10%; **suppressed if cascade latch active** | rate-limited log + status; **no buy pause** |

Cooldown default: 30 minutes (`SPOT_SOFT_ALERT_COOLDOWN_SEC`).
