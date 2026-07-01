# Enhancements Task List

## Phase 1: Robustness & Safety
- [x] 1. risk_agent.py — Implement public fallback regime check `_get_btc_regime_fallback()`
- [x] 1. risk_agent.py — Integrate fallback check in `evaluate()`
- [x] 3. risk_agent.py — Remove static group checks in `check_correlation()` to make correlation dynamic
- `[x]` Implement `_load_live_portfolio_state` in `bounty_hunter.py` and track running metrics dynamically in the scan loop.
- `[x]` Track running metrics dynamically in `run_all_assets` in `orchestrator.py`.
- `[x]` Implement execution-time cash and position limits validation in `scheduler.py` (both signal cycle and bounty hunter cycle).
- `[x]` Run syntax checks and verify unit tests.
- [x] Run 45 iterations of individual weight optimizations for only primary traded assets (BTC/USDT, SOL/USDT, ETH/USDT).
- [x] Summarize and walkthrough changes.
