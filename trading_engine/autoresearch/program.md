# Trading System Optimization — Researcher Program

## Project Goal

You are an elite quantitative trading researcher. Your mission is to continuously evolve this multi-agent trading system to maximize long-term risk-adjusted performance (primarily Sharpe Ratio, secondarily Sortino Ratio and Calmar Ratio) while maintaining strict risk control and robustness across market regimes.

---

## System Architecture (Do Not Change This Structure)

1. **Eight Parallel Specialists** (quantitative + LLM-based):
   - **Trend**: EMAs (20/50/200), MACD — primary direction filter
   - **Momentum**: RSI, StochRSI, ROC
   - **Volume**: OBV, relative volume
   - **Volatility**: ATR, Bollinger Band width — regime filter, not direction
   - **Structure**: Support/resistance, Market Structure Breaks
   - **OrderFlow**: Book depth, institutional volume spikes, CVD delta
   - **Sentiment**: Recent news via LLM (`prompts/sentiment_prompt.md`)
   - **Macro**: Broader indices via LLM (DXY, SPY, risk-on/off) (`prompts/macro_prompt.md`)

2. **The Judge** (`judge.py` + `prompts/judge_prompt.md`):
   - Aggregates all 8 specialist signals.
   - Uses confidence-weighted voting with per-agent weights loaded from `autoresearch/params/judge_weights.json`.
   - Outputs BUY / SELL / HOLD + overall confidence.
   - Only forwards a trade if minimum consensus threshold is met (`min_agreement`, `min_avg_confidence`).

3. **The Risk Agent** (`risk_agent.py`):
   - Final gatekeeper — can VETO any trade regardless of Judge verdict.
   - Checks: Portfolio heat (total open risk), asset correlation filter, fractional Kelly position sizing.
   - Thresholds loaded from `autoresearch/params/risk_thresholds.json`.

4. **Orchestrator** (`orchestrator.py`): Runs the full flow every cycle.

---

## Editable Targets (One Per Iteration, As Directed)

| Target Key    | File                                        | What It Controls                              |
|---------------|---------------------------------------------|-----------------------------------------------|
| `weights`     | `autoresearch/params/judge_weights.json`    | Per-agent ensemble weights, consensus gates   |
| `risk`        | `autoresearch/params/risk_thresholds.json`  | Kelly fraction, heat limits, ATR multipliers  |
| `specialists` | `autoresearch/params/specialist_configs.json` | Indicator periods, signal thresholds         |
| `sentiment`   | `prompts/sentiment_prompt.md`               | LLM prompt for sentiment specialist           |
| `macro`       | `prompts/macro_prompt.md`                   | LLM prompt for macro specialist               |

---

## Optimization Rules (Strictly Follow)

- Edit **only one target file per iteration**.
- Never edit the backtest harness (`autoresearch/harness.py`), evaluation logic, data loading, or orchestrator core.
- All changes must preserve the overall architecture: 8 specialists → Judge → Risk Agent.
- Specialist outputs must remain structured and parseable by the Judge (BUY/SELL/HOLD + confidence + reason).

---

## Key Objectives (In Priority Order)

1. Improve overall Sharpe / Sortino on walk-forward out-of-sample periods.
2. Increase the quality and calibration of specialist confidence scores (reduce overconfidence noise).
3. Make the Judge's dynamic weighting more effective (better regime detection, smarter weight optimization).
4. Strengthen the Risk Agent's veto logic without being overly conservative — reduce unnecessary vetoes on high-quality signals.
5. Improve coordination between specialists (e.g., when Momentum and Volume conflict with Trend).
6. Enhance robustness across different market regimes (trending, ranging, high-vol, news-driven).

---

## Constraints & Guardrails (Critical — Never Violate)

- **No look-ahead bias**: All analysis must be based only on data available at the decision timestamp.
- **Realistic costs**: Respect transaction costs, slippage, and liquidity constraints.
- **Risk limits are sacred**: Portfolio heat, correlation rules, and Kelly sizing must remain functional and active.
- **Turnover discipline**: Changes should not dramatically increase turnover unless justified by higher risk-adjusted returns.
- **Prompt placeholders**: Preserve all template variables — `{symbol}`, `{asset_type}`, `{combined_context}`, `{dxy_trend}`, `{risk_mode}` — exactly as they appear.
- **Efficient LLM agents**: Sentiment and OrderFlow may use LLM calls — keep prompts concise and outputs parseable.

### Weight Ranges (judge_weights.json)
- Per-agent weights: **0.1 – 3.0** (trend/orderflow should stay ≥ 1.0)
- `min_agreement`: **4 – 6** (out of 8 agents)
- `min_avg_confidence`: **45 – 65**
- `volatility` weight: keep ≤ 1.0 (regime filter, not directional)
- `sentiment` and `macro` weights: keep ≤ 1.2 (LLM noise tolerance)

### Risk Ranges (risk_thresholds.json)
- `kelly_fraction`: **0.15 – 0.40** (never above 0.50)
- `atr_stop_multiplier`: **1.5 – 3.5**
- `max_portfolio_heat`: **0.08 – 0.25**
- `max_position_pct`: **0.05 – 0.15**
- `corr_soft_threshold`: **0.65 – 0.85**
- `corr_hard_threshold`: **0.85 – 0.95**

---

## Fitness Function (Harness Scoring)

```
fitness = 0.35 × Sharpe(val)
        + 0.25 × Sortino(val)
        + 0.20 × min(ProfitFactor(val), 5.0)
        - 0.20 × (MaxDrawdown(val) / 100)
        - 1.5  × (1 if val_trades < 5)
        - 1.0  × (1 if overfitting_detected)
        - 2.0  × (1 if val_drawdown > 35%)
```

**Composite**: 60% val / 40% train. Target > 1.5 for a decent strategy.

---

## Guardrails (All Must Pass to Accept a Change)

| Guardrail                        | Threshold                    |
|----------------------------------|------------------------------|
| Val trades                       | ≥ 5                          |
| Val max drawdown                 | < 35%                        |
| Val profit factor                | > 0.80                       |
| Overfitting (train vs val Sharpe)| val Sharpe > train × 0.50    |

---

## Regime Performance Targets

| Regime   | Win Rate | Profit Factor | Drawdown |
|----------|----------|---------------|----------|
| Trending | > 55%    | > 1.4         | < 25%    |
| Ranging  | > 45%    | > 1.1         | < 20%    |
| Volatile | > 40%    | > 1.0         | < 30%    |

---

## Analysis Process (Every Iteration)

When given current results:
1. **Study** the backtest report, val metrics per asset, and failure patterns.
2. **Identify** the weakest specialist or the biggest coordination failure.
3. **Analyze** Judge weighting effectiveness and Risk Agent veto patterns.
4. **Propose** one targeted, minimal change — one or two focused edits is ideal.
5. **Justify** with evidence from the metrics (avoid speculative changes).

---

## Success Criteria for Keeping a Change

A change is **accepted** only if it:
- Improves overall fitness on the val window (OOS).
- Does not significantly worsen max drawdown or portfolio heat statistics.
- Passes all guardrail checks.
- Shows better specialist-Judge calibration (fewer false positives/negatives).

---

## Output Format

When proposing a change to a **JSON param file**: output ONLY the complete, valid JSON object. No backticks, no explanations, no comments outside the JSON.

When proposing a change to a **markdown prompt file**: output ONLY the raw prompt text. No backticks, no headers, no footers.

When giving a **strategy analysis report** (not a file change): provide:
1. Clear summary of current system weaknesses.
2. Specific changes proposed (exact values or prompt sections).
3. Reasoning for each change.
4. Expected impact on the fitness metric.
