# Lessons Learned & Operational Rules

## Order Execution Rules
1. **Never Sell at Raw Market/Best Bid**:
   - When closing or rebalancing positions, always place limit sell orders at or above the desired price target (e.g. `cost_basis * 1.015` or higher).
   - Never cross the spread by selling directly into the bid unless explicitly required.
2. **Reconciliation Cost Basis Accuracy**:
   - Always query and bind the exact FIFO purchase price directly from Bybit exchange trade history (`fetch_my_trades`) rather than relying on initial static estimates.
   - Do not display rough profit approximations on the dashboard before Bybit execution receipts are finalized to prevent visual number adjustments.
3. **Hard Profit Floor**:
   - Every completed sell order must yield $\ge \$0.50$ net profit after all maker/taker fees.

## Reporting Discipline & Reality Checks
1. **Bag Cost Basis vs Fill Price**:
   - Never report an asset as "in profit" merely because mark price is above the latest buy fill. The engine must clear the entire lot's FIFO cost basis before an exit triggers. Report bag P&L honestly relative to the FIFO average cost.
2. **Code Truth over Narrative**:
   - Do not invent narratives about "support sniping" or "order blocks". Describe only the mathematical indicators evaluated in code (4h SMA200, ADX, ATR, fee-proof floors).
3. **Process vs Outcome**:
   - The engine's entry gates improve probability and enforce downside limits, but they cannot ensure or guarantee market bounces. Process is guaranteed; bounces are not.
4. **No Unsolicited P&L Cheerleading / Discrepant Stats**:
   - Answer ONLY what the user asked. Never tack on unsolicited daily profit or cycle statistics to the end of responses.
   - Never quote background reconciler numbers that diverge from the user's live dashboard view or Bybit account reality. If the user asks for numbers, cite what is visible to the user and acknowledge any discrepancy directly.

